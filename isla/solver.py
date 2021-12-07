import copy
import heapq
import itertools
import logging
import math
import sys
import time
from functools import reduce, lru_cache
from typing import Generator, Dict, List, Set, Optional, Tuple, Union, cast

import z3
from fuzzingbook.GrammarCoverageFuzzer import GrammarCoverageFuzzer
from fuzzingbook.GrammarFuzzer import GrammarFuzzer
from fuzzingbook.Grammars import is_nonterminal
from fuzzingbook.Parser import canonical, EarleyParser
from grammar_graph import gg
from grammar_graph.gg import GrammarGraph
from grammar_to_regex.cfg2regex import RegexConverter

import isla.isla_shortcuts as sc
from isla import isla
from isla.existential_helpers import insert_tree
from isla.helpers import delete_unreachable, dict_of_lists_to_list_of_dicts, \
    replace_line_breaks, z3_subst, z3_solve, weighted_geometric_mean, assertions_activated, split_str_with_nonterminals, \
    z3_or, cluster_by_common_elements
from isla.isla import DerivationTree, VariablesCollector, split_conjunction, split_disjunction, \
    convert_to_dnf, convert_to_nnf, ensure_unique_bound_variables, parse_isla, get_conjuncts, set_smt_auto_eval
from isla.type_defs import Grammar, Path


class SolutionState:
    def __init__(self, constraint: isla.Formula, tree: DerivationTree, level: int = 0):
        self.constraint = constraint
        self.tree = tree
        self.level = level
        self.__hash = None

    def formula_satisfied(self, grammar: Grammar) -> isla.ThreeValuedTruth:
        if self.tree.is_open():
            # Have to instantiate variables first
            return isla.ThreeValuedTruth.unknown()

        return isla.evaluate(self.constraint, self.tree, grammar)

    def complete(self) -> bool:
        if not self.tree.is_complete():
            return False

        # We assume that any universal quantifier has already been instantiated, if it matches,
        # and is thus satisfied, or another unsatisfied constraint resulted from the instantiation.
        # Existential, predicate, and SMT formulas have to be eliminated first.

        # return any(all(not isinstance(conjunct, isla.StructuralPredicateFormula)
        #                and (not isinstance(conjunct, isla.SMTFormula) or conjunct == sc.true())
        #                and not isinstance(conjunct, isla.SemanticPredicateFormula)
        #                and not isinstance(conjunct, isla.ExistsFormula)
        #                and (not isinstance(conjunct, isla.ForallFormula) or len(conjunct.already_matched) > 0)
        #                for conjunct in split_conjunction(disjunct))
        #            for disjunct in split_disjunction(self.constraint))

        return self.constraint == sc.true()

    def variables(self) -> Set[isla.Variable]:
        return set(isla.VariablesCollector.collect(self.constraint))

    def __iter__(self):
        yield self.constraint
        yield self.tree

    # Numeric comparisons are needed for using solution states in the binary heap queue
    def __lt__(self, other: 'SolutionState'):
        return hash(self) < hash(other)

    def __le__(self, other: 'SolutionState'):
        return hash(self) <= hash(other)

    def __gt__(self, other: 'SolutionState'):
        return hash(self) > hash(other)

    def __ge__(self, other: 'SolutionState'):
        return hash(self) >= hash(other)

    def __hash__(self):
        if self.__hash is None:
            self.__hash = hash((self.constraint, self.tree))
        return self.__hash

    def __eq__(self, other):
        return (isinstance(other, SolutionState)
                and self.constraint == other.constraint
                and self.tree.structurally_equal(other.tree))

    def __repr__(self):
        return f"SolutionState({repr(self.constraint)}, {repr(self.tree)})"

    def __str__(self):
        return f"{{{self.constraint}, {replace_line_breaks(str(self.tree))}}}"


class CostWeightVector:
    def __init__(
            self,
            tree_closing_cost: float = 0,
            vacuous_penalty: float = 0,
            constraint_cost: float = 0,
            derivation_depth_penalty: float = 0,
            low_k_coverage_penalty: float = 0,
            low_global_k_path_coverage_penalty: float = 0):
        self.tree_closing_cost = tree_closing_cost
        self.vacuous_penalty = vacuous_penalty
        self.constraint_cost = constraint_cost
        self.derivation_depth_penalty = derivation_depth_penalty
        self.low_k_coverage_penalty = low_k_coverage_penalty
        self.low_global_k_path_coverage_penalty = low_global_k_path_coverage_penalty

    def __iter__(self):
        return iter([
            self.tree_closing_cost,
            self.vacuous_penalty,
            self.constraint_cost,
            self.derivation_depth_penalty,
            self.low_k_coverage_penalty,
            self.low_global_k_path_coverage_penalty
        ])

    def __getitem__(self, item):
        assert isinstance(item, int)
        return [
            self.tree_closing_cost,
            self.vacuous_penalty,
            self.constraint_cost,
            self.derivation_depth_penalty,
            self.low_k_coverage_penalty,
            self.low_global_k_path_coverage_penalty
        ][item]

    def __repr__(self):
        return (f"CostWeightVector(tree_closing_cost = {self.tree_closing_cost}, "
                f"vacuous_penalty = {self.vacuous_penalty}, "
                f"constraint_cost = {self.constraint_cost}, "
                f"derivation_depth_penalty = {self.derivation_depth_penalty}, "
                f"low_k_coverage_penalty = {self.low_k_coverage_penalty}, "
                f"low_global_k_path_coverage_penalty = {self.low_global_k_path_coverage_penalty})")


class CostSettings:
    def __init__(
            self,
            weight_vectors: Tuple[CostWeightVector, ...],
            cost_phase_lengths: Tuple[int, ...],
            k: int = 3):
        assert len(weight_vectors) == len(cost_phase_lengths)
        self.weight_vectors = weight_vectors
        self.cost_phase_lengths = cost_phase_lengths
        self.k = k

    def __repr__(self):
        return f"CostSettings(weight_vectors={repr(self.weight_vectors)}, " \
               f"cost_phase_length={repr(self.cost_phase_lengths)}), " \
               f"k={self.k}"


STD_COST_SETTINGS = CostSettings(
    (
        CostWeightVector(
            tree_closing_cost=11,
            vacuous_penalty=0,
            constraint_cost=3,
            derivation_depth_penalty=5,
            low_k_coverage_penalty=20,
            low_global_k_path_coverage_penalty=10),
    ),
    (200,),
    k=3
)


class ISLaSolver:
    def __init__(self,
                 grammar: Grammar,
                 formula: Union[isla.Formula, str],
                 structural_predicates: Optional[Set[isla.StructuralPredicate]] = None,
                 semantic_predicates: Optional[Set[isla.SemanticPredicate]] = None,
                 max_number_free_instantiations: int = 10,
                 max_number_smt_instantiations: int = 10,
                 max_number_tree_insertion_results: int = 5,
                 expand_after_existential_elimination: bool = False,  # Currently not used, might be removed
                 enforce_unique_trees_in_queue: bool = True,
                 precompute_reachability: bool = False,
                 debug: bool = False,
                 cost_settings: CostSettings = STD_COST_SETTINGS,
                 timeout_seconds: Optional[int] = None):
        """
        :param grammar: The underlying grammar.
        :param formula: The formula to solve.
        :param max_number_free_instantiations: Number of times that nonterminals that are not bound by any formula
        should be expanded by a coverage-based fuzzer.
        :param max_number_smt_instantiations: Number of solutions of SMT formulas that should be produced.
        :param expand_after_existential_elimination: Trees are expanded after an existential quantifier elimination
        iff this paramter is set to true. If false, only a finite (potentially small) set of inputs is generated for
        existential constraints. (CURRENTLY NOT USED, MIGHT BE REMOVED)
        :param enforce_unique_trees_in_queue: If true, only one state in the queue containing a tree with the same
        structure can be present at a time. Should be set to false especially if there are top-level SMT formulas
        about numeric constants. TODO: This parameter is awkward, maybe we can find a different solution.
        :param precompute_reachability: If true, the distances between all grammar nodes are pre-computed using
        Floyd-Warshall's algorithm. This makes sense if there are many expensive distance queries, e.g., in a big
        grammar and a constraint with relatively many universal quantifiers.
        :param debug: If true, debug information about the evolution of states is collected, notably in the
        field state_tree. The root of the tree is in the field state_tree_root. The field costs stores the computed
        cost values for all new nodes.
        """
        self.logger = logging.getLogger(type(self).__name__)

        self.grammar = grammar
        self.graph = GrammarGraph.from_grammar(grammar)
        self.canonical_grammar = canonical(grammar)
        self.symbol_costs: Dict[str, int] = self.compute_symbol_costs()
        self.timeout_seconds = timeout_seconds

        if isinstance(formula, str):
            formula = parse_isla(formula, structural_predicates, semantic_predicates)

        self.formula = ensure_unique_bound_variables(formula)
        top_constants: Set[isla.Constant] = set(
            [c for c in VariablesCollector.collect(self.formula)
             if isinstance(c, isla.Constant)
             and not c.is_numeric()])
        assert len(top_constants) == 1
        self.top_constant = next(iter(top_constants))

        quantifier_chains: List[Tuple[isla.ForallFormula, ...]] = [
            tuple([f for f in c if isinstance(f, isla.ForallFormula)])
            for c in get_quantifier_chains(formula)]
        self.quantifier_chains: List[Tuple[isla.ForallFormula, ...]] = [c for c in quantifier_chains if c]

        self.max_number_free_instantiations: int = max_number_free_instantiations
        self.max_number_smt_instantiations: int = max_number_smt_instantiations
        self.max_number_tree_insertion_results = max_number_tree_insertion_results
        # self.expand_after_existential_elimination = expand_after_existential_elimination
        self.enforce_unique_trees_in_queue = enforce_unique_trees_in_queue

        self.cost_settings = cost_settings
        self.current_cost_phase: int = 0
        self.current_cost_phase_since: int = 0
        self.covered_k_paths: Set[Tuple[gg.Node, ...]] = set()

        self.precompute_reachability = precompute_reachability
        if precompute_reachability:
            # Pre-compute grammar graph distances for more performant reachability queries
            self.logger.info("Pre-Computing shortest distances between all grammar nodes "
                             "for quicker reachability queries...")
            node_distances: [Dict[gg.Node, Dict[gg.Node, int]]] = self.graph.shortest_distances(infinity=sys.maxsize)
            u: gg.Node
            self.node_distances: [Dict[str, Dict[str, int]]] = {
                u.symbol: {
                    v.symbol: dist
                    for v, dist in node_distances[u].items()
                    if not isinstance(v, gg.ChoiceNode)
                }
                for u in self.graph.all_nodes
                if type(u) is gg.NonterminalNode
            }
            self.logger.info("DONE Pre-Computing shortest distances between all grammar nodes.")

        # Initialize Queue
        initial_tree = DerivationTree(self.top_constant.n_type, None)
        initial_formula = self.formula.substitute_expressions({self.top_constant: initial_tree})
        initial_state = SolutionState(initial_formula, initial_tree)
        initial_states = self.establish_invariant(initial_state)

        self.queue: List[Tuple[float, SolutionState]] = []
        self.tree_hashes_in_queue: Set[int] = {initial_tree.structural_hash()}
        for state in initial_states:
            heapq.heappush(self.queue, (self.compute_cost(state), state))
        self.current_level = 0

        # Debugging stuff
        self.debug = debug
        self.state_tree: Dict[SolutionState, List[SolutionState]] = {}  # is only filled if self.debug
        self.state_tree_root = None
        self.current_state = None
        self.costs: Dict[SolutionState, float] = {}

        if self.debug:
            self.state_tree[initial_state] = initial_states
            self.state_tree_root = initial_state
            self.costs[initial_state] = 0
            for state in initial_states:
                self.costs[state] = self.compute_cost(state)

    def solve(self) -> Generator[DerivationTree, None, None]:
        start_time = int(time.time())

        while self.queue:
            if self.timeout_seconds is not None:
                if int(time.time()) - start_time > self.timeout_seconds:
                    return

            # import dill as pickle
            # state_hash = -6081099422178527529
            # out_file = "/tmp/saved_debug_state"
            # if hash(self.queue[0][1]) == state_hash:
            #     with open(out_file, 'wb') as debug_state_file:
            #         pickle.dump(self, debug_state_file)
            #     print(f"Dumping state to {out_file}")
            #     exit()

            cost: int
            state: SolutionState
            cost, state = heapq.heappop(self.queue)
            self.current_level = state.level
            self.tree_hashes_in_queue.discard(state.tree.structural_hash())

            if self.debug:
                self.current_state = state
                self.state_tree.setdefault(state, [])
            self.logger.debug(f"Polling new state %s (hash %d, cost %f)", state, hash(state), cost)
            self.logger.debug(f"Queue length: %s", len(self.queue))

            assert not isinstance(state.constraint, isla.DisjunctiveFormula)

            # Instantiate all top-level structural predicate formulas.
            state = self.instantiate_structural_predicates(state)

            if state.constraint == sc.false():
                continue

            # Instantiate numeric constant introduction
            result_state = self.instantiate_numeric_constant_intros(state)
            if result_state is not None:
                yield from self.process_new_state(result_state)
                continue

            # Match all universal formulas
            result_states = self.match_all_universal_formulas(state)
            if result_states is not None:
                yield from self.process_new_states(result_states)
                continue

            # Expand if there are any not yet eliminated / matched universal quantifiers
            if any(isinstance(conjunct, isla.ForallFormula) for conjunct in get_conjuncts(state.constraint)):
                expanded_states = self.expand_tree(state)
                assert len(expanded_states) > 0, f"State {state} will never leave the queue."

                self.logger.debug("Expanding state %s (%d successors)", state, len(expanded_states))
                yield from self.process_new_states(expanded_states)
                continue

            # Eliminate all semantic formulas
            result_states = self.eliminate_all_semantic_formulas(state)
            if result_states is not None:
                yield from self.process_new_states(result_states)
                continue

            # Eliminate all ready semantic predicate formulas
            result_state = self.eliminate_all_ready_semantic_predicate_formulas(state)
            if result_state is not None:
                yield from self.process_new_state(result_state)
                continue

            # Eliminate first existential formula
            result_states = self.eliminate_first_match_all_existential_formulas(state)
            if result_states is not None:
                yield from self.process_new_states(result_states)
                continue
                # if (not self.expand_after_existential_elimination
                #         or any(result_state.constraint == sc.true() for result_state in result_states)):
                #     continue

            # semantic predicate formulas can remain if they bind lazily. In that case, we can choose a random
            # instantiation and let the predicate "fix" the resulting tree.
            assert (state.constraint == sc.true() or
                    all(isinstance(conjunct, isla.SemanticPredicateFormula)
                        for conjunct in get_conjuncts(state.constraint))), \
                f"Constraint is not true and contains formulas " \
                f"other than semantic predicate formulas: {state.constraint}"
            assert (state.constraint == sc.true() or
                    all(not pred_formula.binds_tree(leaf)
                        for pred_formula in get_conjuncts(state.constraint)
                        if isinstance(pred_formula, isla.SemanticPredicateFormula)
                        for _, leaf in state.tree.open_leaves())), \
                f"Constraint is not true and contains semantic predicate formulas which bind open leaves in the tree: " \
                f"{state.constraint}, leaves: {', '.join(list(map(str, [leaf for _, leaf in state.tree.open_leaves()])))}"

            fuzzer = GrammarCoverageFuzzer(self.grammar)
            if state.constraint == sc.true():
                for _ in range(self.max_number_free_instantiations):
                    result = state.tree
                    for path, leaf in state.tree.open_leaves():
                        leaf_inst = DerivationTree.from_parse_tree(fuzzer.expand_tree((leaf.value, None)))
                        result = result.replace_path(path, leaf_inst)
                    yield from self.process_new_states([SolutionState(state.constraint, result)])
            else:
                for _ in range(self.max_number_free_instantiations):
                    substitutions: Dict[DerivationTree, DerivationTree] = {
                        subtree: DerivationTree.from_parse_tree(fuzzer.expand_tree((subtree.value, None)))
                        for path, subtree in state.tree.open_leaves()
                    }

                    if substitutions:
                        yield from self.process_new_states([
                            SolutionState(
                                state.constraint.substitute_expressions(substitutions),
                                state.tree.substitute(substitutions))])

    def instantiate_structural_predicates(self, state: SolutionState) -> SolutionState:
        predicate_formulas = [
            pred_formula for pred_formula in isla.FilterVisitor(
                lambda f: isinstance(f, isla.StructuralPredicateFormula)).collect(state.constraint)
            if (isinstance(pred_formula, isla.StructuralPredicateFormula)
                and all(not isinstance(arg, isla.Variable) for arg in pred_formula.args))
        ]

        formula = state.constraint
        for predicate_formula in predicate_formulas:
            instantiation = isla.SMTFormula(z3.BoolVal(predicate_formula.evaluate(state.tree)))
            self.logger.debug("Eliminating (-> %s) structural predicate formula %s", instantiation, predicate_formula)
            formula = isla.replace_formula(formula, predicate_formula, instantiation)

        return SolutionState(formula, state.tree)

    def instantiate_numeric_constant_intros(self, state: SolutionState) -> Optional[SolutionState]:
        numeric_constant_introductions = [
            conjunct for conjunct in get_conjuncts(state.constraint)
            if isinstance(conjunct, isla.IntroduceNumericConstantFormula)]

        if not numeric_constant_introductions:
            return None

        formula = state.constraint
        for numeric_constant_introduction in numeric_constant_introductions:
            self.logger.debug("Instantiating numeric constant introduction %s", numeric_constant_introduction)
            used_vars = set(VariablesCollector.collect(formula))
            fresh = isla.fresh_constant(
                used_vars,
                isla.Constant(
                    numeric_constant_introduction.bound_variable.name,
                    numeric_constant_introduction.bound_variable.n_type))
            instantiation = numeric_constant_introduction.inner_formula.substitute_variables(
                {numeric_constant_introduction.bound_variable: fresh})
            formula = isla.replace_formula(formula, numeric_constant_introduction, instantiation)

        return SolutionState(formula, state.tree)

    def eliminate_all_semantic_formulas(self, state: SolutionState) -> Optional[List[SolutionState]]:
        conjuncts = split_conjunction(state.constraint)
        semantic_formulas = [conjunct for conjunct in conjuncts
                             if isinstance(conjunct, isla.SMTFormula)
                             and not z3.is_true(conjunct.formula)]

        if not semantic_formulas:
            return None

        self.logger.debug("Eliminating semantic formulas [%s]", ", ".join(map(str, semantic_formulas)))

        prefix_conjunction = reduce(lambda a, b: a & b, semantic_formulas, sc.true())
        new_disjunct = (
                prefix_conjunction &
                reduce(lambda a, b: a & b,
                       [conjunct for conjunct in conjuncts if conjunct not in semantic_formulas],
                       sc.true()))

        return self.eliminate_semantic_formula(
            prefix_conjunction,
            SolutionState(new_disjunct, state.tree))

    def eliminate_all_ready_semantic_predicate_formulas(self, state: SolutionState) -> Optional[SolutionState]:
        semantic_predicate_formulas = [
            pred_formula for pred_formula in isla.FilterVisitor(
                lambda f: isinstance(f, isla.SemanticPredicateFormula)).collect(state.constraint)
            if (isinstance(pred_formula, isla.SemanticPredicateFormula)
                and all(not isinstance(arg, isla.Variable) for arg in pred_formula.args))
        ]

        semantic_predicate_formulas = sorted(semantic_predicate_formulas, key=lambda f: f.order)

        if not semantic_predicate_formulas:
            return None

        result = state

        changed = False
        for idx, semantic_predicate_formula in enumerate(semantic_predicate_formulas):
            evaluation_result = semantic_predicate_formula.evaluate(self.grammar)
            if not evaluation_result.ready():
                continue

            self.logger.debug("Eliminating semantic predicate formula %s", semantic_predicate_formula)
            changed = True

            if evaluation_result.false():
                return SolutionState(sc.false(), result.tree)

            if evaluation_result.true():
                result = SolutionState(
                    isla.replace_formula(
                        result.constraint,
                        semantic_predicate_formula,
                        sc.true()),
                    result.tree)
                continue

            new_constraint = (
                isla.replace_formula(result.constraint, semantic_predicate_formula, sc.true())
                    .substitute_expressions(evaluation_result.result))

            for k in range(idx + 1, len(semantic_predicate_formulas)):
                semantic_predicate_formulas[k] = cast(
                    isla.SemanticPredicateFormula,
                    semantic_predicate_formulas[k].substitute_expressions(evaluation_result.result))

            result = SolutionState(
                new_constraint,
                result.tree.substitute(evaluation_result.result))

        return result if changed else None

    def eliminate_first_match_all_existential_formulas(self, state: SolutionState) -> Optional[List[SolutionState]]:
        existential_formulas = [
            conjunct for conjunct in split_conjunction(state.constraint)
            if isinstance(conjunct, isla.ExistsFormula)]

        if not existential_formulas:
            return None

        # We can actually match *all* existential formulas; only with tree insertion, we perform one step at a time.
        new_states = [state]
        for existential_formula in existential_formulas:
            next_round_states = []
            while new_states:
                s = new_states.pop()
                match_result = self.match_existential_formula(existential_formula, s)
                if match_result:
                    next_round_states.extend(match_result)
                else:
                    next_round_states.append(s)
            new_states = next_round_states

        if new_states == [state]:
            new_states = []

        if new_states:
            self.logger.debug(
                "Matched existential formulas [%s], result: [%s]",
                ", ".join(map(str, existential_formulas)),
                ", ".join([f"{s} (hash={hash(s)})" for s in new_states])
            )

        complete_states = [state for state in new_states
                           if state.complete()
                           or state.constraint == sc.true()]
        if complete_states:
            self.logger.debug("Matching existential formula %s", existential_formulas[0])
            new_states = complete_states
        else:
            elimination_result = self.eliminate_existential_formula(existential_formulas[0], state)
            new_states.extend(elimination_result)
            self.logger.debug(
                "Eliminating existential formula %s, %d successors", existential_formulas[0], len(elimination_result))

        return new_states

    def match_all_universal_formulas(self, state: SolutionState) -> Optional[List[SolutionState]]:
        universal_formulas = [
            conjunct for conjunct in split_conjunction(state.constraint)
            if isinstance(conjunct, isla.ForallFormula)]

        if not universal_formulas:
            return None

        result = self.match_universal_formulas(universal_formulas, state)
        if result:
            self.logger.debug("Matched universal formulas [%s]", ", ".join(map(str, universal_formulas)))

        return result or None

    def expand_tree(self, state: SolutionState) -> List[SolutionState]:
        """
        Expands the given tree, but not at nonterminals that can be freely instantiated of those that directly
        correspond to the assignment constant.

        :param state: The current state.
        :return: A (possibly empty) list of expanded trees.
        """

        nonterminal_expansions = {
            leaf_path: [
                [DerivationTree(child, None if is_nonterminal(child) else [])
                 for child in expansion]
                for expansion in self.canonical_grammar[leaf_node.value]
            ]
            for leaf_path, leaf_node in state.tree.open_leaves()
            if any(
                self.quantified_formula_might_match(formula, leaf_path, state.tree)
                for formula in get_conjuncts(state.constraint)
                if isinstance(formula, isla.ForallFormula))}

        possible_expansions: List[Dict[Path, List[DerivationTree]]] = \
            dict_of_lists_to_list_of_dicts(nonterminal_expansions)

        assert len(possible_expansions) == math.prod(len(values) for values in nonterminal_expansions.values())

        if len(possible_expansions) == 1 and not possible_expansions[0]:
            return []

        result: List[SolutionState] = []

        for possible_expansion in possible_expansions:
            expanded_tree = state.tree
            for path, new_children in possible_expansion.items():
                leaf_node = expanded_tree.get_subtree(path)
                expanded_tree = expanded_tree.replace_path(
                    path, DerivationTree(leaf_node.value, new_children, leaf_node.id))

                assert expanded_tree is not state.tree
                assert expanded_tree != state.tree
                assert expanded_tree.structural_hash() != state.tree.structural_hash()

            updated_constraint = state.constraint.substitute_expressions({
                state.tree.get_subtree(path[:idx]): expanded_tree.get_subtree(path[:idx])
                for path in possible_expansion
                for idx in range(len(path) + 1)
            })

            result.append(SolutionState(updated_constraint, expanded_tree))

        return result

    def match_universal_formulas(
            self, universal_formulas: List[isla.ForallFormula], state: SolutionState) -> List[SolutionState]:
        number_matches = 0
        context_formula = state.constraint

        for universal_formula in universal_formulas:
            matches: List[Dict[isla.Variable, Tuple[Path, DerivationTree]]] = \
                [match for match in isla.matches_for_quantified_formula(universal_formula, self.grammar)
                 if not universal_formula.is_already_matched(match[universal_formula.bound_variable][1])]

            universal_formula_with_matches = universal_formula.add_already_matched({
                match[universal_formula.bound_variable][1]
                for match in matches
            })

            for match in matches:
                number_matches += 1

                inst_formula = universal_formula_with_matches.inner_formula.substitute_expressions({
                    variable: match_tree for variable, (_, match_tree) in match.items()
                })

                context_formula = inst_formula & isla.replace_formula(
                    context_formula, universal_formula, universal_formula_with_matches
                )

        if number_matches:
            return [SolutionState(context_formula, state.tree)]
        else:
            return []

    def match_existential_formula(
            self, existential_formula: isla.ExistsFormula, state: SolutionState) -> List[SolutionState]:
        result: List[SolutionState] = []

        matches: List[Dict[isla.Variable, Tuple[Path, DerivationTree]]] = \
            isla.matches_for_quantified_formula(existential_formula, self.grammar)

        for match in matches:
            inst_formula = existential_formula.inner_formula.substitute_expressions({
                variable: match_tree for variable, (_, match_tree) in match.items()
            })
            constraint = inst_formula & isla.replace_formula(state.constraint, existential_formula, sc.true())
            result.append(SolutionState(constraint, state.tree))

        return result

    def eliminate_existential_formula(
            self, existential_formula: isla.ExistsFormula, state: SolutionState) -> List[SolutionState]:
        if existential_formula.bind_expression is not None:
            inserted_trees_and_bind_paths = existential_formula.bind_expression.to_tree_prefix(
                existential_formula.bound_variable.n_type, self.grammar)
        else:
            inserted_trees_and_bind_paths = [(DerivationTree(existential_formula.bound_variable.n_type, None), {})]

        result: List[SolutionState] = []

        inserted_tree: DerivationTree
        bind_expr_paths: Dict[isla.BoundVariable, Path]
        for inserted_tree, bind_expr_paths in inserted_trees_and_bind_paths:
            self.logger.debug(
                "insert_tree(self.canonical_grammar, %s, %s, self.graph, %s)",
                repr(inserted_tree),
                repr(existential_formula.in_variable),
                self.max_number_tree_insertion_results)

            insertion_results = insert_tree(
                self.canonical_grammar,
                inserted_tree,
                existential_formula.in_variable,
                graph=self.graph,
                max_num_solutions=self.max_number_tree_insertion_results * 2
            )

            insertion_results = sorted(insertion_results, key=lambda t: compute_tree_closing_cost(t, self.symbol_costs))
            insertion_results = insertion_results[:self.max_number_tree_insertion_results]

            for insertion_result in insertion_results:
                # actual_inserted_tree = insertion_result.get_subtree(insertion_result.find_node(inserted_tree))
                resulting_tree = state.tree.replace_path(
                    state.tree.find_node(existential_formula.in_variable),
                    insertion_result)

                tree_substitution: Dict[DerivationTree, DerivationTree] = {
                    original_tree: resulting_tree.get_subtree(original_path)
                    for original_path, original_tree in state.tree.paths()
                    if (resulting_tree.is_valid_path(original_path) and
                        original_tree.value == resulting_tree.get_subtree(original_path).value and
                        not resulting_tree.get_subtree(original_path).structurally_equal(original_tree))
                }

                assert insertion_result.find_node(inserted_tree) is not None
                variable_substitutions = {existential_formula.bound_variable: inserted_tree}

                if bind_expr_paths:
                    if assertions_activated():
                        dangling_bind_expr_vars = [
                            (var, path)
                            for var, path in bind_expr_paths.items()
                            if (var in existential_formula.bind_expression.bound_variables() and
                                insertion_result.find_node(inserted_tree.get_subtree(path)) is None)]
                        assert not dangling_bind_expr_vars, \
                            f"Bound variables from match expression not found in tree: " \
                            f"[{' ,'.join(map(repr, dangling_bind_expr_vars))}]"

                    variable_substitutions.update({
                        var: inserted_tree.get_subtree(path)
                        for var, path in bind_expr_paths.items()
                        if var in existential_formula.bind_expression.bound_variables()
                    })

                instantiated_formula = existential_formula.inner_formula.substitute_expressions(variable_substitutions)

                new_formula = (
                        instantiated_formula &
                        self.formula.substitute_expressions(
                            {self.top_constant: state.tree.substitute(tree_substitution)}) &
                        isla.replace_formula(
                            state.constraint,
                            existential_formula,
                            sc.true()).substitute_expressions(tree_substitution))

                new_state = SolutionState(new_formula, state.tree.substitute(tree_substitution))

                if assertions_activated() or self.debug:
                    lost_tree_predicate_arguments: List[DerivationTree] = [
                        arg
                        for invstate in self.establish_invariant(new_state)
                        for predicate_formula in get_conjuncts(invstate.constraint)
                        if isinstance(predicate_formula, isla.StructuralPredicateFormula)
                        for arg in predicate_formula.args
                        if isinstance(arg, DerivationTree) and
                           invstate.tree.find_node(arg) is None]

                    if lost_tree_predicate_arguments:
                        previous_posititions = [state.tree.find_node(t) for t in lost_tree_predicate_arguments]
                        assert False, f"Dangling subtrees [{', '.join(map(repr, lost_tree_predicate_arguments))}], " \
                                      f"previously at positions [{', '.join(map(str, previous_posititions))}] " \
                                      f"in tree {repr(state.tree)} (hash: {hash(state)})."

                    lost_semantic_formula_arguments = [
                        arg
                        for invstate in self.establish_invariant(new_state)
                        for semantic_formula in get_conjuncts(new_state.constraint)
                        if isinstance(semantic_formula, isla.SMTFormula)
                        for arg in semantic_formula.substitutions.values()
                        if invstate.tree.find_node(arg) is None]

                    if lost_semantic_formula_arguments:
                        previous_posititions = [state.tree.find_node(t) for t in lost_semantic_formula_arguments]
                        assert False, f"Dangling subtrees [{', '.join(map(repr, lost_semantic_formula_arguments))}], " \
                                      f"previously at positions [{', '.join(map(str, previous_posititions))}] " \
                                      f"in tree {repr(state.tree)} (hash: {hash(state)})."

                result.append(new_state)

        if not result:
            self.logger.warning(
                "Existential qfr elimination: Could not insert any tree in %s into tree %s",
                [str(inserted_tree) for inserted_tree, _ in inserted_trees_and_bind_paths],
                state.tree)

        return result

    def eliminate_semantic_formula(self, semantic_formula: isla.Formula, state: SolutionState) -> List[SolutionState]:
        """
        Solves a semantic formula and, for each solution, substitutes the solution for the respective
        constant in each assignment of the state. Also instantiates all "free" constants in the given
        tree. The SMT solver is passed a regular expression approximating the language of the nonterminal
        of each considered constant. Returns an empty list for unsolvable constraints.

        :param semantic_formula: The semantic (i.e., only containing logical connectors and SMT Formulas)
        formula to solve.
        :param state: The original solution state.
        :return: A list of instantiated SolutionStates.
        """

        assert all(isinstance(conjunct, isla.SMTFormula) for conjunct in get_conjuncts(semantic_formula))

        # NODE: We need to cluster SMT formulas by tree substitutions. If there are two formulas
        # with a variable $var which is instantiated to different trees, we need two separate
        # solutions. If, however, $var is instantiated with the *same* tree, we need one solution
        # to both formulas together.

        smt_formulas = self.rename_instantiated_variables_in_smt_formulas(
            [smt_formula for smt_formula in get_conjuncts(semantic_formula) if
             isinstance(smt_formula, isla.SMTFormula)])

        # Now, we also cluster formulas by common variables (and instantiated subtrees: One formula
        # might yield an instantiation of a subtree of the instantiation of another formula. They
        # need to appear in the same cluster). The solver can better handle smaller constraints,
        # and those which do not have variables in common can be handled independently.

        formula_clusters: List[List[isla.SMTFormula]] = cluster_by_common_elements(
            smt_formulas,
            lambda smt_formula: (
                    smt_formula.free_variables() |
                    smt_formula.instantiated_variables |
                    set([subtree for tree in smt_formula.substitutions.values() for _, subtree in tree.paths()])
            )
        )

        assert all(any(smt_formula in cluster for cluster in formula_clusters) for smt_formula in smt_formulas)

        all_solutions: List[List[Dict[Union[isla.Constant, DerivationTree], DerivationTree]]] = [
            self.solve_quantifier_free_formula(cluster) for cluster in formula_clusters
        ]

        # These solutions are all independent, such that we can combine each solution with all others.
        solutions: List[Dict[Union[isla.Constant, DerivationTree], DerivationTree]] = []

        if len(all_solutions) == 1:
            solutions = all_solutions[0]
        else:
            for solutions_1, solutions_2 in itertools.product(all_solutions, all_solutions):
                if solutions_1 is solutions_2:
                    continue

                for solution_1, solution_2 in zip(solutions_1, solutions_2):
                    solutions.append(solution_1 | solution_2)

        solutions_with_subtrees: List[Dict[Union[isla.Constant, DerivationTree], DerivationTree]] = []
        for solution in solutions:
            # We also have to instantiate all subtrees of the substituted element.

            solution_with_subtrees: Dict[Union[isla.Constant, DerivationTree], DerivationTree] = {}
            for orig, subst in solution.items():
                if isinstance(orig, isla.Constant):
                    solution_with_subtrees[orig] = subst
                    continue

                assert isinstance(orig, DerivationTree)
                for path, tree in [(p, t) for p, t in orig.paths() if t not in solution_with_subtrees]:
                    assert subst.is_valid_path(path), \
                        f"SMT Solution {subst} does not have " \
                        f"orig path {path} from tree {orig} (state {hash(state)})"
                    solution_with_subtrees[tree] = subst.get_subtree(path)

            solutions_with_subtrees.append(solution_with_subtrees)

        results = []
        for solution in solutions_with_subtrees:
            if solution:
                new_state = SolutionState(
                    state.constraint.substitute_expressions(solution),
                    state.tree.substitute(solution))
            else:
                new_state = SolutionState(
                    isla.replace_formula(state.constraint, semantic_formula, sc.true()),
                    state.tree)

            results.append(new_state)

        return results

    def solve_quantifier_free_formula(self, smt_formulas: List[isla.SMTFormula]) \
            -> List[Dict[Union[isla.Constant, DerivationTree], DerivationTree]]:
        # If any SMT formula refers to subtrees in the instantiations of other SMT formulas,
        # we have to instantiate those first.
        def get_conflicts(smt_formulas):
            return [
                f for fidx, f in enumerate(smt_formulas)
                if any(
                    ot != t and ot.find_node(t) is not None
                    for t in f.substitutions.values()
                    for ofidx, of in enumerate(smt_formulas)
                    if fidx != ofidx
                    for ot in of.substitutions.values())]

        conflicts = get_conflicts(smt_formulas)

        if conflicts:
            smt_formulas = conflicts
            assert not get_conflicts(smt_formulas)

        tree_substitutions = reduce(
            lambda d1, d2: d1 | d2,
            [smt_formula.substitutions for smt_formula in smt_formulas],
            {}
        )

        constants = reduce(
            lambda d1, d2: d1 | d2,
            [smt_formula.free_variables() | smt_formula.instantiated_variables
             for smt_formula in smt_formulas],
            set()
        )

        solutions: List[Dict[Union[isla.Constant, DerivationTree], DerivationTree]] = []
        internal_solutions: List[Dict[isla.Constant, z3.StringVal]] = []

        for _ in range(self.max_number_smt_instantiations):
            formulas: List[z3.BoolRef] = []

            for constant in constants:
                if constant.is_numeric():
                    regex = z3.Union(z3.Re("0"), z3.Concat(z3.Range("1", "9"), z3.Star(z3.Range("0", "9"))))
                elif constant in tree_substitutions:
                    # We have a more concrete shape of the desired instantiation available
                    regexes = [
                        self.extract_regular_expression(t) if is_nonterminal(t)
                        else z3.Re(t)
                        for t in split_str_with_nonterminals(str(tree_substitutions[constant]))]
                    assert regexes
                    regex = z3.Concat(*regexes) if len(regexes) > 1 else regexes[0]
                else:
                    regex = self.extract_regular_expression(constant.n_type)

                formulas.append(z3.InRe(z3.String(constant.name), regex))

            for prev_solution in internal_solutions:
                formulas.append(z3_or([
                    z3.Not(constant.to_smt() == string_val)
                    for constant, string_val in prev_solution.items()]))

            for smt_formula in smt_formulas:
                formulas.append(smt_formula.formula)

            solver_result, maybe_model = z3_solve(formulas)

            if solver_result != z3.sat:
                if not solutions:
                    return []
                else:
                    return solutions

            assert maybe_model is not None

            new_solution = {
                tree_substitutions.get(constant, constant):
                    (
                        val := maybe_model[z3.String(constant.name)].as_string(),
                        DerivationTree(val, []) if constant.is_numeric() else self.parse(constant.n_type, val)
                    )[-1]
                for constant in constants
            }

            new_internal_solution = {
                constant: z3.StringVal(maybe_model[z3.String(constant.name)].as_string())
                for constant in constants}

            if new_solution in solutions:
                # This can happen for trivial solutions, i.e., if the formula is logically valid.
                # Then, the assignment for that constant will always be {}
                return solutions
            else:
                solutions.append(new_solution)
                internal_solutions.append(new_internal_solution)

        return solutions

    def rename_instantiated_variables_in_smt_formulas(self, smt_formulas):
        old_smt_formulas = smt_formulas
        smt_formulas = []
        for subformula in old_smt_formulas:
            subst_var: isla.Variable
            subst_tree: DerivationTree

            new_smt_formula: z3.BoolRef = subformula.formula
            new_substitutions = subformula.substitutions
            new_instantiated_variables = subformula.instantiated_variables

            for subst_var, subst_tree in subformula.substitutions.items():
                new_name = f"{subst_tree.value}_{subst_tree.id}"
                new_var = isla.BoundVariable(new_name, subst_var.n_type)

                new_smt_formula = cast(z3.BoolRef, z3_subst(new_smt_formula, {subst_var.to_smt(): new_var.to_smt()}))
                new_substitutions = {new_var if k == subst_var else k: v for k, v in new_substitutions.items()}
                new_instantiated_variables = {new_var if v == subst_var else v for v in new_instantiated_variables}

            smt_formulas.append(
                isla.SMTFormula(
                    new_smt_formula,
                    *subformula.free_variables_,
                    instantiated_variables=new_instantiated_variables,
                    substitutions=new_substitutions))

        return smt_formulas

    def process_new_states(self, new_states: List[SolutionState]) -> List[DerivationTree]:
        result = [tree for new_state in new_states for tree in self.process_new_state(new_state)]

        for tree in [state.tree for state in new_states]:
            self.covered_k_paths.update(tree.k_paths(self.graph, self.cost_settings.k))

        if self.covered_k_paths == self.graph.k_paths(self.cost_settings.k):
            self.covered_k_paths = set()
            # self.logger.info("ALL COVERED")
        else:
            pass
            # uncovered_paths = self.graph.k_paths(self.cost_settings.k) - self.covered_k_paths
            # self.logger.info("\n".join([", ".join(f"'{n.symbol}'" for n in p) for p in uncovered_paths]))

        return result

    def process_new_state(self, new_state: SolutionState) -> List[DerivationTree]:
        new_state = self.instantiate_structural_predicates(new_state)
        new_states = self.establish_invariant(new_state)
        new_states = [self.remove_nonmatching_universal_quantifiers(new_state) for new_state in new_states]
        new_states = [self.remove_infeasible_universal_quantifiers(new_state) for new_state in new_states]

        return [new_state.tree for new_state in new_states if self.state_is_valid_or_enqueue(new_state)]

    def state_is_valid_or_enqueue(self, state: SolutionState) -> bool:
        """
        Returns True if the given state is valid, such that it can be yielded. Returns False and enqueues the state
        if the state is not yet complete, otherwise returns False and discards the state.
        """

        if state.complete():
            assert state.formula_satisfied(self.grammar).is_true()
            return True

        # Helps in debugging below assertion:
        # [(predicate_formula, [
        #     arg for arg in predicate_formula.args
        #     if isinstance(arg, DerivationTree) and not state.tree.find_node(arg)])
        #  for predicate_formula in get_conjuncts(state.constraint)
        #  if isinstance(predicate_formula, isla.StructuralPredicateFormula)]

        if assertions_activated() or self.debug:
            dangling_predicate_argument_trees = [
                (predicate_formula, arg)
                for predicate_formula in isla.FilterVisitor(
                    lambda f: isinstance(f, isla.StructuralPredicateFormula)).collect(state.constraint)
                for arg in cast(isla.StructuralPredicateFormula, predicate_formula).args
                if isinstance(arg, DerivationTree) and state.tree.find_node(arg) is None
            ]

            if dangling_predicate_argument_trees:
                assert False, \
                    "Dangling predicate arguments: [" + \
                    ", ".join([str(f) + ", " + repr(a) for f, a in dangling_predicate_argument_trees]) + "]"

            dangling_smt_formula_argument_trees = [
                (smt_formula, arg)
                for smt_formula in isla.FilterVisitor(
                    lambda f: isinstance(f, isla.SMTFormula)).collect(state.constraint)
                for arg in cast(isla.SMTFormula, smt_formula).substitutions.values()
                if isinstance(arg, DerivationTree) and state.tree.find_node(arg) is None
            ]

            if dangling_smt_formula_argument_trees:
                assert False, \
                    "Dangling SMT formula arguments: [" + \
                    ", ".join([str(f) + ", " + repr(a) for f, a in dangling_smt_formula_argument_trees]) + "]"

        if self.enforce_unique_trees_in_queue and state.tree.structural_hash() in self.tree_hashes_in_queue:
            # Some structures can arise as well from tree insertion (existential quantifier elimination)
            # and expansion; also, tree insertion can yield different trees that have intersecting
            # expansions. We drop those to output more diverse solutions (numbers for SMT solutions
            # and free nonterminals are configurable, so you get more outputs by playing with those!).
            self.logger.debug("Discarding state %s, tree already in queue", state)
            return False

        if state.constraint == sc.false():
            self.logger.debug("Discarding state %s", state)
            return False

        state = SolutionState(state.constraint, state.tree, level=self.current_level + 1)

        cost = self.compute_cost(state)
        heapq.heappush(self.queue, (cost, state))
        self.tree_hashes_in_queue.add(state.tree.structural_hash())

        if self.debug:
            self.state_tree[self.current_state].append(state)
            self.costs[state] = cost

        self.logger.debug("Pushing new state %s (hash %d, cost %f)", state, hash(state), cost)
        self.logger.debug("Queue length: %d", len(self.queue))
        if len(self.queue) % 100 == 0:
            self.logger.info("Queue length: %d", len(self.queue))

        # if self.queue_size_limit is not None and len(queue) > self.queue_size_limit:
        #     self.logger.debug(f"Balancing queue")
        #     nlargest = heapq.nlargest(self.queue_no_removed_items, queue)
        #     for elem in nlargest:
        #         queue.remove(elem)
        #     heapq.heapify(queue)

        return False

    def establish_invariant(self, state: SolutionState) -> List[SolutionState]:
        formula = convert_to_dnf(convert_to_nnf(state.constraint))
        return [
            SolutionState(disjunct, state.tree)
            for disjunct in split_disjunction(formula)]

    def compute_cost(self, state: SolutionState) -> float:
        """Cost of state. Best value: 0, Worst: Unbounded"""

        if state.constraint == sc.true():
            return 0

        if (len(self.queue) - self.current_cost_phase_since >
                self.cost_settings.cost_phase_lengths[self.current_cost_phase]):
            self.current_cost_phase_since = len(self.queue)
            self.current_cost_phase = (self.current_cost_phase + 1) % len(self.cost_settings.weight_vectors)
            self.logger.debug(
                "Switching to cost phase %d of %d, vector %s",
                self.current_cost_phase + 1,
                len(self.cost_settings.weight_vectors),
                str(self.cost_settings.weight_vectors[self.current_cost_phase])
            )

        cost_weight_vector = self.cost_settings.weight_vectors[self.current_cost_phase]
        symbol_costs = self.symbol_costs
        grammar = self.grammar
        graph = self.graph
        k = self.cost_settings.k

        return compute_cost(
            state, symbol_costs, cost_weight_vector, k, self.covered_k_paths,
            self.formula, grammar, graph, self.quantifier_chains)

    def compute_symbol_costs(self) -> Dict[str, int]:
        self.logger.info("Computing symbol costs")
        result: Dict[str, int] = {}

        for nonterminal in self.grammar:
            fuzzer = GrammarFuzzer(self.grammar)
            tree = fuzzer.expand_tree_with_strategy((nonterminal, None), fuzzer.expand_node_max_cost, 1)
            tree = fuzzer.expand_tree_with_strategy(tree, fuzzer.expand_node_min_cost)
            result[nonterminal] = sum([
                len([expansion for expansion in self.canonical_grammar[tree.value]
                     if any(is_nonterminal(symbol) for symbol in expansion)])
                for _, tree in DerivationTree.from_parse_tree(tree).paths()
                if is_nonterminal(tree.value)
            ])

        def all_paths(
                from_node: gg.NonterminalNode,
                to_node: gg.NonterminalNode, cycles_allowed: int = 1) -> List[List[gg.NonterminalNode]]:
            """Compute all paths between two nodes. Note: We allow to visit each nonterminal twice.
            This is not really allowing up to `cycles_allowed` cycles (which was the original intention
            of the parameter), since then we would have to check per path; yet, the number of paths would
            explode then and the current implementation provides reasonably good results."""
            result: List[List[gg.NonterminalNode]] = []
            visited: Dict[gg.NonterminalNode, int] = {n: 0 for n in self.graph.all_nodes}

            queue: List[List[gg.NonterminalNode]] = [[from_node]]
            while queue:
                p = queue.pop(0)
                if p[-1] == to_node:
                    result.append(p)
                    continue

                for child in p[-1].children:
                    if not isinstance(child, gg.NonterminalNode) or visited[child] > cycles_allowed + 1:
                        continue

                    visited[child] += 1
                    queue.append(p + [child])

            return [[n for n in p if not isinstance(n, gg.ChoiceNode)] for p in result]

        nonterminal_parents = [
            nonterminal for nonterminal in self.canonical_grammar
            if any(
                is_nonterminal(symbol)
                for expansion in self.canonical_grammar[nonterminal]
                for symbol in expansion)]

        # Sometimes this computation results in some nonterminals having lower cost values
        # than nonterminals that are reachable from those (but not vice versa), which is
        # undesired. We counteract this by assuring that on paths with at most one cycle
        # from the root to any nonterminal parent, the costs are strictly monotonically
        # decreasing.
        for nonterminal_parent in nonterminal_parents:
            for path in all_paths(self.graph.root, self.graph.get_node(nonterminal_parent)):
                for idx in reversed(range(1, len(path))):
                    source: gg.Node = path[idx - 1]
                    target: gg.Node = path[idx]

                    if result[source.symbol] <= result[target.symbol]:
                        result[source.symbol] = result[target.symbol] + 1

        return result

    def remove_nonmatching_universal_quantifiers(self, state: SolutionState) -> SolutionState:
        result = state
        for universal_formula in [conjunct for conjunct in get_conjuncts(state.constraint)
                                  if isinstance(conjunct, isla.ForallFormula)]:
            if (universal_formula.in_variable.is_complete()
                    and not isla.matches_for_quantified_formula(universal_formula, self.grammar)):
                result = SolutionState(
                    isla.replace_formula(result.constraint, universal_formula, sc.true()),
                    result.tree)

        return result

    def remove_infeasible_universal_quantifiers(self, state: SolutionState) -> SolutionState:
        result = state
        for universal_formula in get_conjuncts(state.constraint):
            if not isinstance(universal_formula, isla.ForallFormula):
                continue

            if not (any(self.quantified_formula_might_match(universal_formula, leaf_path, universal_formula.in_variable)
                        for leaf_path, leaf_node in universal_formula.in_variable.open_leaves())
                    or [match for match in isla.matches_for_quantified_formula(universal_formula, self.grammar)
                        if not universal_formula.is_already_matched(match[universal_formula.bound_variable][1])]):
                result = SolutionState(
                    isla.replace_formula(result.constraint, universal_formula, sc.true()),
                    result.tree)

        return result

    def quantified_formula_might_match(
            self, qfd_formula: isla.QuantifiedFormula, path_to_nonterminal: Path, tree: DerivationTree) -> bool:
        return isla.quantified_formula_might_match(
            qfd_formula,
            path_to_nonterminal,
            tree,
            self.grammar,
            self.reachable)

    @lru_cache(maxsize=None)
    def node_distance(self, nonterminal: str, to_nonterminal: str) -> int:
        if self.precompute_reachability:
            return self.node_distances[nonterminal][to_nonterminal]
        else:
            from_node = self.graph.get_node(nonterminal)
            to_node = self.graph.get_node(to_nonterminal)

            assert from_node is not None, f"Node {nonterminal} not found in graph"
            assert to_node is not None, f"Node {to_nonterminal} not found in graph"

            return self.graph.dijkstra(from_node, to_node)[0][to_node]

    def reachable(self, nonterminal: str, to_nonterminal: str) -> bool:
        return self.node_distance(nonterminal, to_nonterminal) < sys.maxsize

    @lru_cache(maxsize=None)
    def extract_regular_expression(self, nonterminal: str) -> z3.ReRef:
        regex_conv = RegexConverter(self.grammar, compress_unions=True)
        return regex_conv.to_regex(nonterminal)

    def parse(self, nonterminal: str, input: str) -> DerivationTree:
        grammar = copy.deepcopy(self.grammar)
        grammar["<start>"] = [nonterminal]
        delete_unreachable(grammar)
        parser = EarleyParser(grammar)
        return DerivationTree.from_parse_tree(list(parser.parse(input))[0][1][0])


def compute_cost(
        state: SolutionState,
        symbol_costs: Dict[str, int],
        cost_weight_vector: CostWeightVector,
        k: int,
        covered_k_paths: Set[Tuple[gg.Node, ...]],
        orig_formula: isla.Formula,
        grammar: Grammar,
        graph: gg.GrammarGraph,
        quantifier_chains: List[Tuple[isla.ForallFormula, ...]]) -> float:
    # How costly is it to finish the tree?
    tree_closing_cost = compute_tree_closing_cost(state.tree, symbol_costs)

    # Quantifiers are expensive (universal formulas have to be matched, tree insertion for existential
    # formulas is even more costly). TODO: Penalize nested quantifiers more.
    constraint_cost = sum(
        [idx * (2 if isinstance(f, isla.ExistsFormula) else 1) + 1
         for c in get_quantifier_chains(state.constraint)
         for idx, f in enumerate(c)])

    # How far are we from matching a yet unmatched universal quantifier?
    # vacuous_penalty = compute_match_dist(state, grammar, node_distance)

    # What is the proportion of vacuously satisfied universal quantifiers *chains* in (extensions of) this tree?
    vacuous_penalty = (
        0 if not cost_weight_vector.vacuous_penalty else
        compute_vacuous_penalty(grammar, graph, orig_formula, state.tree, quantifier_chains))

    # k-Path coverage: Fewer covered -> higher penalty
    k_cov_cost = compute_k_coverage_cost(graph, k, state)

    # Covered k-paths: Fewer contributed -> higher penalty
    global_k_path_cost = compute_global_k_coverage_cost(covered_k_paths, graph, k, state)

    costs = [tree_closing_cost, vacuous_penalty, constraint_cost, state.level, k_cov_cost, global_k_path_cost]
    assert all(c >= 0 for c in costs)

    # Compute geometric mean
    result = weighted_geometric_mean(costs, list(cost_weight_vector))

    logging.getLogger(type(ISLaSolver).__name__).debug(
        "Computed cost for state %s:\n%f, individual costs: %s, weights: %s",
        f"({(str(state.constraint)[:50] + '...') if len(str(state.constraint)) > 53 else str(state.constraint)}, "
        f"{state.tree})", result, costs, cost_weight_vector)

    return result


def compute_tree_closing_cost(tree: DerivationTree, symbol_costs: Dict[str, int]) -> float:
    nonterminals = [leaf.value for _, leaf in tree.open_leaves()]
    return sum([symbol_costs[nonterminal] for nonterminal in nonterminals])


def compute_global_k_coverage_cost(
        covered_k_paths: Set[Tuple[gg.Node, ...]], graph: gg.GrammarGraph, k: int, state: SolutionState):
    num_contributed_k_paths = len(
        {path for path in graph.k_paths(k)
         if path in state.tree.k_paths(graph, k) and
         path not in covered_k_paths})
    num_missing_k_paths = len(graph.k_paths(k)) - len(covered_k_paths)

    return 1 - (num_contributed_k_paths / num_missing_k_paths)


def compute_k_coverage_cost(graph: GrammarGraph, k: int, state: SolutionState) -> float:
    return math.prod([1 - state.tree.k_coverage(graph, k) for k in range(1, k + 1)]) ** (1 / float(k))


def get_quantifier_chains(formula: isla.Formula) -> \
        List[Tuple[Union[isla.QuantifiedFormula, isla.IntroduceNumericConstantFormula], ...]]:
    univ_toplevel_formulas = isla.get_toplevel_quantified_formulas(formula)
    return [(f,) + c for f in univ_toplevel_formulas for c in (get_quantifier_chains(f.inner_formula) or [()])]


def compute_vacuous_penalty(
        grammar: Grammar,
        graph: gg.GrammarGraph,
        orig_formula: isla.Formula,
        tree: DerivationTree,
        quantifier_chains: List[Tuple[isla.ForallFormula, ...]]) -> float:
    # Returns a value between 0 (best) and 1 (worst).
    formula = isla.instantiate_top_constant(orig_formula, tree)
    toplevel_qfrs = isla.get_toplevel_quantified_formulas(formula)

    if not quantifier_chains:
        return 0

    set_smt_auto_eval(formula, False)

    vacuously_matched_quantifiers = set()
    isla.eliminate_quantifiers(
        formula,
        vacuously_satisfied=vacuously_matched_quantifiers,
        grammar=grammar,
        graph=graph)

    set_smt_auto_eval(formula, True)

    vacuous_chains = {
        c for c in quantifier_chains if
        all(any(of.id == f.id for of in vacuously_matched_quantifiers)
            for f in c)}

    # Only consider one chain per top-level quantifier
    vacuous_chains = set([
        chains[0] for chains in
        set([tuple(d for d in vacuous_chains
                   if d[0].id == c[0].id)
             for c in vacuous_chains])])

    vacuous_penalty = len(vacuous_chains) / len(toplevel_qfrs)
    assert 0 <= vacuous_penalty <= 1

    return vacuous_penalty