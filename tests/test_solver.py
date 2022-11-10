# Copyright © 2022 CISPA Helmholtz Center for Information Security.
# Author: Dominic Steinhöfel.
#
# This file is part of ISLa.
#
# ISLa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ISLa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with ISLa.  If not, see <http://www.gnu.org/licenses/>.

import functools
import heapq
import logging
import os
import random
import string
import sys
import unittest
from typing import cast, Optional, Dict, List, Callable, Union, Set
from xml.dom import minidom
from xml.sax.saxutils import escape

import pytest
import z3
from grammar_graph import gg
from orderedset import OrderedSet

import isla.derivation_tree
import isla.evaluator
from isla import isla_shortcuts as sc
from isla import language
from isla.derivation_tree import DerivationTree
from isla.existential_helpers import DIRECT_EMBEDDING, SELF_EMBEDDING, CONTEXT_ADDITION
from isla.fuzzer import GrammarFuzzer, GrammarCoverageFuzzer
from isla.helpers import crange, Exceptional, Maybe, to_id, canonical
from isla.isla_predicates import (
    BEFORE_PREDICATE,
    COUNT_PREDICATE,
    STANDARD_SEMANTIC_PREDICATES,
    STANDARD_STRUCTURAL_PREDICATES,
)
from isla.language import (
    VariablesCollector,
    parse_isla,
    start_constant,
    SemanticPredicate,
    SemPredEvalResult,
    parse_bnf,
)
from isla.parser import EarleyParser, PEGParser
from isla.solver import (
    ISLaSolver,
    SolutionState,
    CostSettings,
    CostWeightVector,
    get_quantifier_chains,
    CostComputer,
    GrammarBasedBlackboxCostComputer,
    implies,
    equivalent,
    SolverTimeout,
    UnknownResultError,
    SemanticError,
    create_fixed_length_tree,
    generate_abstracted_trees,
)
from isla.type_defs import Grammar, ImmutableList
from isla.z3_helpers import z3_eq, smt_string_val_to_string
from isla_formalizations import rest, tar, simple_tar, scriptsizec
from isla_formalizations.csv import csv_lint, CSV_GRAMMAR, CSV_HEADERBODY_GRAMMAR
from isla_formalizations.tar import extract_tar
from isla_formalizations.xml_lang import (
    XML_GRAMMAR_WITH_NAMESPACE_PREFIXES,
    XML_NAMESPACE_CONSTRAINT,
    XML_WELLFORMEDNESS_CONSTRAINT,
    XML_GRAMMAR,
    validate_xml,
    XML_NO_ATTR_REDEF_CONSTRAINT,
)
from test_data import LANG_GRAMMAR, SIMPLE_CSV_GRAMMAR, CONFIG_GRAMMAR


class TestSolver(unittest.TestCase):
    def test_atomic_smt_formula(self):
        assgn = language.Constant("$assgn", "<assgn>")
        formula = language.SMTFormula(
            z3_eq(assgn.to_smt(), z3.StringVal("x := x")), assgn
        )
        self.execute_generation_test(formula, num_solutions=1)

    def test_simple_universal_formula(self):
        start = language.Constant("$start", "<start>")
        var1 = language.BoundVariable("$var", "<var>")

        formula = sc.forall(
            var1,
            start,
            sc.smt_for(cast(z3.BoolRef, z3_eq(var1.to_smt(), z3.StringVal("x"))), var1),
        )

        self.execute_generation_test(formula, max_number_free_instantiations=1)

    def test_simple_universal_formula_with_bind(self):
        mgr = language.VariableManager(LANG_GRAMMAR)
        formula = mgr.create(
            sc.forall_bind(
                language.BindExpression(mgr.bv("$var1", "<var>")),
                mgr.bv("$rhs", "<rhs>"),
                mgr.const("$start", "<start>"),
                mgr.smt(
                    cast(z3.BoolRef, z3_eq(mgr.bv("$var1").to_smt(), z3.StringVal("x")))
                ),
            )
        )

        self.execute_generation_test(formula)

    def test_simple_existential_formula(self):
        mgr = language.VariableManager(LANG_GRAMMAR)
        start = language.Constant("$start", "<start>")

        formula = mgr.create(
            sc.exists(
                mgr.bv("$var", "<var>"),
                start,
                mgr.smt(z3_eq(mgr.bv("$var").to_smt(), z3.StringVal("x"))),
            )
        )

        self.execute_generation_test(
            formula,
            num_solutions=50,
            max_number_free_instantiations=1,
            enforce_unique_trees_in_queue=False,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=5,
                        constraint_cost=0,
                        derivation_depth_penalty=10,
                        low_k_coverage_penalty=20,
                        low_global_k_path_coverage_penalty=40,
                    ),
                    k=4,
                ),
                gg.GrammarGraph.from_grammar(LANG_GRAMMAR),
            ),
        )

    def test_simple_existential_formula_with_bind(self):
        start = language.Constant("$start", "<start>")
        rhs = language.BoundVariable("$rhs", "<rhs>")
        var1 = language.BoundVariable("$var", "<var>")

        formula = sc.exists_bind(
            language.BindExpression(var1),
            rhs,
            start,
            sc.smt_for(z3_eq(var1.to_smt(), z3.StringVal("x")), var1),
        )

        self.execute_generation_test(formula, num_solutions=50)

    def test_conjunction_of_qfd_formulas(self):
        start = language.Constant("$start", "<start>")
        assgn = language.BoundVariable("$assgn", "<assgn>")
        rhs_1 = language.BoundVariable("$rhs_1", "<rhs>")
        rhs_2 = language.BoundVariable("$rhs_2", "<rhs>")
        var_1 = language.BoundVariable("$var1", "<var>")
        var_2 = language.BoundVariable("$var2", "<var>")

        formula = sc.forall_bind(
            language.BindExpression(var_1),
            rhs_1,
            start,
            sc.smt_for(z3_eq(var_1.to_smt(), z3.StringVal("x")), var_1),
        ) & sc.forall_bind(
            var_2 + " := " + rhs_2,
            assgn,
            start,
            sc.smt_for(z3_eq(var_2.to_smt(), z3.StringVal("y")), var_2),
        )

        self.execute_generation_test(formula)

    def test_xml(self):
        constraint = """
forall <xml-tree> tree="<{<id> opid}[ <xml-attribute>]><inner-xml-tree></{<id> clid}>" in start:
    (= opid clid)"""

        self.execute_generation_test(
            constraint,
            grammar=XML_GRAMMAR,
            max_number_free_instantiations=1,
            num_solutions=30,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=16,
                        constraint_cost=7,
                        derivation_depth_penalty=13,
                        low_k_coverage_penalty=26,
                        low_global_k_path_coverage_penalty=20,
                    )
                ),
                gg.GrammarGraph.from_grammar(XML_GRAMMAR),
            ),
        )

    def test_get_quantifier_chains(self):
        chains_1 = get_quantifier_chains(XML_WELLFORMEDNESS_CONSTRAINT)
        self.assertEqual(1, len(chains_1))
        chains_2 = get_quantifier_chains(XML_NAMESPACE_CONSTRAINT)
        self.assertEqual(2, len(chains_2))
        all_chains = get_quantifier_chains(
            XML_WELLFORMEDNESS_CONSTRAINT & XML_NAMESPACE_CONSTRAINT
        )
        self.assertEqual(3, len(all_chains))
        self.assertEqual(set(chains_1) | set(chains_2), set(all_chains))

    def test_xml_with_prefixes(self):
        # TODO: Check how we can generate (more) prefixed attributes.
        self.execute_generation_test(
            XML_NAMESPACE_CONSTRAINT
            & XML_WELLFORMEDNESS_CONSTRAINT
            & XML_NO_ATTR_REDEF_CONSTRAINT,
            grammar=XML_GRAMMAR_WITH_NAMESPACE_PREFIXES,
            max_number_free_instantiations=1,
            num_solutions=50,
            enforce_unique_trees_in_queue=True,
            custom_test_func=validate_xml,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=10,
                        constraint_cost=0,
                        derivation_depth_penalty=6,
                        low_k_coverage_penalty=0,
                        low_global_k_path_coverage_penalty=13,
                    ),
                    k=4,
                ),
                gg.GrammarGraph.from_grammar(XML_GRAMMAR_WITH_NAMESPACE_PREFIXES),
            ),
        )

    def test_declared_before_used(self):
        mgr = language.VariableManager(LANG_GRAMMAR)
        formula: language.Formula = mgr.create(
            sc.forall_bind(
                mgr.bv("$lhs_1", "<var>") + " := " + mgr.bv("$rhs_1", "<rhs>"),
                mgr.bv("$assgn_1", "<assgn>"),
                mgr.const("$start", "<start>"),
                sc.forall(
                    mgr.bv("$var", "<var>"),
                    mgr.bv("$rhs_1"),
                    sc.exists_bind(
                        mgr.bv("$lhs_2", "<var>") + " := " + mgr.bv("$rhs_2", "<rhs>"),
                        mgr.bv("$assgn_2", "<assgn>"),
                        mgr.const("$start"),
                        sc.before(mgr.bv("$assgn_2"), mgr.bv("$assgn_1"))
                        & mgr.smt(
                            z3_eq(mgr.bv("$lhs_2").to_smt(), mgr.bv("$var").to_smt())
                        ),
                    ),
                ),
            )
        )

        self.execute_generation_test(
            formula,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=1,
            num_solutions=40,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=7,
                        constraint_cost=5,
                        derivation_depth_penalty=15,
                        low_k_coverage_penalty=20,
                        low_global_k_path_coverage_penalty=10,
                    ),
                    k=3,
                ),
                gg.GrammarGraph.from_grammar(LANG_GRAMMAR),
            ),
        )

    def test_declared_before_used_concrete_syntax(self):
        formula = """
forall <assgn> assgn_1="<var> := {<var> rhs}" in start:
  exists <assgn> assgn_2="{<var> lhs} := <rhs>" in start:
    (before(assgn_2, assgn_1) and (= lhs rhs))
"""

        self.execute_generation_test(
            formula,
            structural_predicates={BEFORE_PREDICATE},
            max_number_free_instantiations=1,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=7,
                        constraint_cost=3.25,
                        derivation_depth_penalty=15,
                        low_k_coverage_penalty=21.5,
                        low_global_k_path_coverage_penalty=12.5,
                    ),
                    k=4,
                ),
                gg.GrammarGraph.from_grammar(LANG_GRAMMAR),
            ),
            num_solutions=50,
        )

    def test_solve_assgn_lang_without_constraint(self):
        self.execute_generation_test(
            max_number_free_instantiations=10,
            num_solutions=10,
        )

    def test_solve_assgn_lang_without_constraint_low_free_instantiations(self):
        try:
            self.execute_generation_test(
                max_number_free_instantiations=5,
                num_solutions=10,
            )
        except AssertionError as aerr:
            self.assertIn("Only found 5 solutions", str(aerr))

    def test_declared_before_used_concrete_simplified_syntax(self):
        formula = """
exists <assgn> assgn:
  (before(assgn, <assgn>) and <assgn>.<rhs>.<var> = assgn.<var>)
"""

        grammar = f"""<start> ::= <stmt>
<stmt> ::= <assgn> | <assgn> " ; " <stmt>
<assgn> ::= <var> " := " <rhs>
<rhs> ::= <var> | <digit>
<var> ::= {' | '.join(map(lambda c: f'"{c}"', string.ascii_lowercase))}
<digit> ::= {' | '.join(map(lambda c: f'"{c}"', string.digits))}"""

        self.execute_generation_test(
            formula,
            grammar=grammar,
            structural_predicates={BEFORE_PREDICATE},
            max_number_free_instantiations=1,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=7,
                        constraint_cost=3.25,
                        derivation_depth_penalty=15,
                        low_k_coverage_penalty=21.5,
                        low_global_k_path_coverage_penalty=12.5,
                    ),
                    k=4,
                ),
                gg.GrammarGraph.from_grammar(LANG_GRAMMAR),
            ),
            num_solutions=50,
        )

    def test_simple_csv_rows_equal_length(self):
        property = """
forall <csv-header> hline in start:
  exists int colno:
    ((>= (str.to.int colno) 3) and 
    ((<= (str.to.int colno) 5) and 
     (count(hline, "<csv-field>", colno) and 
     forall <csv-record> line in start:
       count(line, "<csv-field>", colno))))
"""

        self.execute_generation_test(
            property,
            grammar=SIMPLE_CSV_GRAMMAR,
            semantic_predicates={COUNT_PREDICATE},
            max_number_free_instantiations=1,
            max_number_smt_instantiations=3,
            enforce_unique_trees_in_queue=False,
            num_solutions=20,
        )

    def test_csv_rows_equal_length_simpler(self):
        property = """
exists int num:
  forall <csv-record> elem in start:
    ((>= (str.to.int num) 1) and
     count(elem, "<raw-field>", num))"""

        self.execute_generation_test(
            property,
            semantic_predicates={COUNT_PREDICATE},
            grammar=CSV_GRAMMAR,
            custom_test_func=csv_lint,
            num_solutions=30,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=2,
            enforce_unique_trees_in_queue=False,
            global_fuzzer=False,
            fuzzer_factory=functools.partial(
                GrammarFuzzer, min_nonterminals=0, max_nonterminals=30
            ),
        )

    def test_csv_rows_equal_length(self):
        property = """
forall <csv-header> hline in start:
  exists int colno:
    ((>= (str.to.int colno) 1) and 
    ((<= (str.to.int colno) 5) and 
     (count(hline, "<raw-field>", colno) and 
     forall <csv-record> line in start:
       count(line, "<raw-field>", colno))))
"""

        self.execute_generation_test(
            property,
            semantic_predicates={COUNT_PREDICATE},
            grammar=CSV_GRAMMAR,
            custom_test_func=csv_lint,
            num_solutions=90,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=3,
            enforce_unique_trees_in_queue=False,
            global_fuzzer=False,
            fuzzer_factory=functools.partial(
                GrammarFuzzer, min_nonterminals=0, max_nonterminals=30
            ),
        )

    def test_csv_rows_equal_length_more_complex(self):
        property = """
forall <csv-header> header in start:
  forall <csv-body> body in start:
    forall <csv-record> hline in header:
      exists int colno:
        ((>= (str.to.int colno) 3) and 
         (<= (str.to.int colno) 5) and
         count(hline, "<raw-field>", colno) and 
         forall <csv-record> line in body:
            count(line, "<raw-field>", colno))
"""

        self.execute_generation_test(
            property,
            semantic_predicates={COUNT_PREDICATE},
            grammar=CSV_HEADERBODY_GRAMMAR,
            custom_test_func=csv_lint,
            num_solutions=20,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=1,
            enforce_unique_trees_in_queue=False,
            global_fuzzer=True,
        )

    def test_negated_csv_rows_equal_length(self):
        property = parse_isla(
            """
exists <csv-header> header in start:
  exists <csv-body> body in start:
    exists <csv-record> hline in header:
      forall int colno:
        (not(>= (str.to_int colno) 3) or 
         not(<= (str.to_int colno) 5) or
         not(count(hline, "<raw-field>", colno)) or 
         exists <csv-record> line in body:
           not(count(line, "<raw-field>", colno)))
""",
            CSV_HEADERBODY_GRAMMAR,
            semantic_predicates={COUNT_PREDICATE},
        )

        # We don't find infinite solutions here, problem with existentially quantified top-level formulas...
        self.execute_generation_test(
            property,
            semantic_predicates={COUNT_PREDICATE},
            grammar=CSV_HEADERBODY_GRAMMAR,
            custom_test_func=lambda t: isinstance(csv_lint(t), str),
            num_solutions=32,  # Max number reachable
            max_number_free_instantiations=2,
            max_number_smt_instantiations=1,
            enforce_unique_trees_in_queue=False,
            global_fuzzer=True,
            debug=True,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=7,
                        constraint_cost=5,
                        derivation_depth_penalty=15,
                        low_k_coverage_penalty=20,
                        low_global_k_path_coverage_penalty=10,
                    ),
                    k=3,
                ),
                gg.GrammarGraph.from_grammar(CSV_HEADERBODY_GRAMMAR),
            ),
        )

    def test_simple_equal_length_csv_negated(self):
        # Original property:
        # exists int colno:
        #   forall <csv-record> record in start:
        #     count(record, "<raw-field>", colno)
        property = """
forall int colno:
  exists <csv-record> record in start:
    not(count(record, "<raw-field>", colno))"""

        self.execute_generation_test(
            property,
            grammar=CSV_GRAMMAR,
            custom_test_func=lambda t: isinstance(csv_lint(t), str),
            num_solutions=40,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=1,
            enforce_unique_trees_in_queue=False,
            global_fuzzer=True,
            debug=True,
        )

    @pytest.mark.flaky(reruns=3, reruns_delay=2)
    def test_rest(self):
        random.seed(10)
        self.execute_generation_test(
            rest.LENGTH_UNDERLINE
            & rest.DEF_LINK_TARGETS
            & rest.NO_LINK_TARGET_REDEF
            & rest.LIST_NUMBERING_CONSECUTIVE,
            custom_test_func=rest.render_rst,
            grammar=rest.REST_GRAMMAR,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=1,
            num_solutions=50,
            enforce_unique_trees_in_queue=True,
            # tree_insertion_methods=0,
            # print_only=True,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=7,
                        constraint_cost=1.5,
                        derivation_depth_penalty=2.5,
                        low_k_coverage_penalty=2,
                        low_global_k_path_coverage_penalty=18,
                    ),
                    k=4,
                ),
                gg.GrammarGraph.from_grammar(rest.REST_GRAMMAR),
                reset_coverage_after_n_round_with_no_coverage=500,
            ),
        )

    def test_scriptsize_c_def_before_use(self):
        self.execute_generation_test(
            scriptsizec.SCRIPTSIZE_C_DEF_USE_CONSTR
            & scriptsizec.SCRIPTSIZE_C_NO_REDEF_CONSTR,
            grammar=scriptsizec.SCRIPTSIZE_C_GRAMMAR,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=2,
            enforce_unique_trees_in_queue=True,
            custom_test_func=scriptsizec.compile_scriptsizec_clang,
            num_solutions=50,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=5,
                        constraint_cost=2,
                        derivation_depth_penalty=6,
                        low_k_coverage_penalty=2,
                        low_global_k_path_coverage_penalty=21,
                    ),
                    k=3,
                ),
                gg.GrammarGraph.from_grammar(scriptsizec.SCRIPTSIZE_C_GRAMMAR),
                reset_coverage_after_n_round_with_no_coverage=100,
            ),
            # print_only=True
        )

    def test_scriptsize_c_redef(self):
        self.execute_generation_test(
            scriptsizec.SCRIPTSIZE_C_NO_REDEF_CONSTR,
            grammar=scriptsizec.SCRIPTSIZE_C_GRAMMAR,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=2,
            enforce_unique_trees_in_queue=True,
            # custom_test_func=scriptsizec.compile_scriptsizec_clang,
            num_solutions=50,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=10,
                        constraint_cost=0,
                        derivation_depth_penalty=9,
                        low_k_coverage_penalty=28,
                        low_global_k_path_coverage_penalty=4,
                    ),
                    k=4,
                ),
                gg.GrammarGraph.from_grammar(scriptsizec.SCRIPTSIZE_C_GRAMMAR),
            ),
            # print_only=True
        )

    @pytest.mark.skip(
        reason="Have to disable assertions to run this test, disabling in CI pipeline."
    )
    def test_tar(self):
        sys.setrecursionlimit(1500)
        self.execute_generation_test(
            tar.TAR_CONSTRAINTS,
            grammar=tar.TAR_GRAMMAR,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=1,
            enforce_unique_trees_in_queue=False,
            # debug=True,
            num_solutions=60,
            custom_test_func=extract_tar,
            cost_computer=GrammarBasedBlackboxCostComputer(
                CostSettings(
                    CostWeightVector(
                        tree_closing_cost=12,
                        constraint_cost=1,
                        derivation_depth_penalty=2,
                        low_k_coverage_penalty=0,
                        low_global_k_path_coverage_penalty=0,
                    ),
                    k=4,
                ),
                gg.GrammarGraph.from_grammar(tar.TAR_GRAMMAR),
            ),
            tree_insertion_methods=DIRECT_EMBEDDING | SELF_EMBEDDING,
        )

    def test_simple_tar(self):
        self.execute_generation_test(
            simple_tar.TAR_CONSTRAINTS,
            grammar=simple_tar.SIMPLE_TAR_GRAMMAR,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=1,
            enforce_unique_trees_in_queue=False,
            debug=True,
            num_solutions=10,
        )

    @staticmethod
    def state_tree_to_xml(
        root: SolutionState,
        tree: Dict[SolutionState, List[SolutionState]],
        costs: Dict[SolutionState, float],
        prettify=True,
        seen: Optional[Set[SolutionState]] = None,
    ) -> str:
        if seen and root in seen:
            return ""

        if root not in tree:
            children_string = ""
        else:
            children_string = (
                "<children>"
                + "".join(
                    [
                        TestSolver.state_tree_to_xml(
                            child, tree, costs, False, (seen or set()) | {root}
                        )
                        for child in tree[root]
                    ]
                )
                + "</children>"
            )

        special_char_map = {
            "\x00": "&lt;NUL&gt;",
            "\x0b": "&lt;VTAB&gt;",
            "\x0c": "&lt;FFEED&gt;",
        }

        result = (
            "<state>"
            + "<constraint>"
            + escape(str(root.constraint), special_char_map)
            + "</constraint>"
            + "<tree>"
            + escape(str(root.tree), special_char_map)
            + "</tree>"
            + "<cost>"
            + str(costs[root])
            + "</cost>"
            + "<hash>"
            + str(hash(root))
            + "</hash>"
            + children_string
            + "</state>"
        )

        if prettify:
            return minidom.parseString(result).toprettyxml(indent="    ")
        else:
            return result

    def test_parse(self):
        constraint = "<pagesize> = <bufsize>"
        solver = ISLaSolver(CONFIG_GRAMMAR, constraint)

        self.assertEqual(
            "pagesize=12\nbufsize=12", str(solver.parse("pagesize=12\nbufsize=12"))
        )

        self.assertTrue(
            Exceptional.of(lambda: solver.parse("Xpagesize=12\nbufsize=12"))
            .map(lambda _: False)
            .recover(lambda e: isinstance(e, SyntaxError))
        )

        self.assertTrue(
            Exceptional.of(lambda: solver.parse("pagesize=12\nbufsize=21"))
            .map(lambda _: False)
            .recover(lambda e: isinstance(e, SemanticError))
        )

    def test_check(self):
        constraint = "<pagesize> = <bufsize>"
        solver = ISLaSolver(CONFIG_GRAMMAR, constraint)

        self.assertTrue(solver.check("pagesize=12\nbufsize=12"))
        self.assertFalse(solver.check("pagesize=12\nbufsize=1200"))

    def test_solve_config_grammar_leaddigit_equality(self):
        # This raised an exception
        solver = ISLaSolver(
            CONFIG_GRAMMAR,
            'forall <int> i="<leaddigit>" in <start>: (i = "7")',
            max_number_free_instantiations=1,
            max_number_smt_instantiations=1,
        )

        for _ in range(10):
            solution = solver.solve()
            logging.getLogger(type(self).__name__).info(f"Found solution: {solution}")
            for _, int_tree in solution.filter(lambda n: n.value == "<int>"):
                self.assertTrue(len(str(int_tree)) > 1 or str(int_tree) == "7")

        solver = ISLaSolver(
            CONFIG_GRAMMAR,
            'forall <int> i="<leaddigit><digits>" in <start>: (i = "7")',
            max_number_free_instantiations=1,
            max_number_smt_instantiations=1,
        )

        for i in range(10):
            try:
                solution = solver.solve()
                logging.getLogger(type(self).__name__).info(
                    f"Found solution: {solution}"
                )
                for _, int_tree in solution.filter(lambda n: n.value == "<int>"):
                    self.assertTrue(str(int_tree) == "7")
            except StopIteration:
                self.assertEqual(1, i)  # Only 1 solution
                break

    def test_check_unknown(self):
        never_ready = SemanticPredicate(
            "neverReady", 1, lambda _, __: SemPredEvalResult(None), binds_tree=True
        )

        solver = ISLaSolver(
            LANG_GRAMMAR, "neverReady(<var>)", semantic_predicates={never_ready}
        )

        self.assertTrue(
            Exceptional.of(lambda: solver.check("x := 1"))
            .map(lambda _: False)
            .recover(lambda e: isinstance(e, UnknownResultError))
            .a
        )

    def test_start_nonterminal(self):
        result = parse_isla('forall <var> in <start>: <var> = "a"')
        expected = parse_isla('forall <var> in start: (= <var> "a")')
        self.assertEqual(expected, result)

    def test_length_indexed_strings(self):
        PASCAL_STRING_GRAMMAR = {
            "<start>": ["<string>"],
            "<string>": ["<length><chars>"],
            "<length>": ["<high-byte><low-byte>"],
            "<high-byte>": ["<byte>"],
            "<low-byte>": ["<byte>"],
            "<byte>": crange("\x00", "\xff"),
            "<chars>": ["", "<char><chars>"],
            "<char>": list(string.printable),
        }

        solver = ISLaSolver(
            PASCAL_STRING_GRAMMAR,
            """
str.to_code(<string>.<length>.<low-byte>) =
str.len(<string>.<chars>) and 
<string>.<length>.<high-byte> = str.from_code(0)""",
        )

        solution = solver.solve()

        high_byte = solution.filter(lambda n: n.value == "<high-byte>")[0][1]
        low_byte = solution.filter(lambda n: n.value == "<low-byte>")[0][1]
        chars = solution.filter(lambda n: n.value == "<chars>")[0][1]

        self.assertEqual(0, ord(str(high_byte)))
        self.assertEqual(len(str(chars)), ord(str(low_byte)))

    def test_unsatisfiable_smt_atom(self):
        solver = ISLaSolver(LANG_GRAMMAR, '<var> = "aa"', activate_unsat_support=True)

        self.assertTrue(
            Exceptional.of(solver.solve)
            .map(lambda _: False)
            .recover(lambda e: isinstance(e, StopIteration))
        )

    def test_unsatisfiable_smt_conjunction(self):
        solver = ISLaSolver(
            LANG_GRAMMAR, '<var> = "a" and <var> = "b"', activate_unsat_support=True
        )

        self.assertTrue(
            Exceptional.of(solver.solve)
            .map(lambda _: False)
            .recover(lambda e: isinstance(e, StopIteration))
        )

    def test_unsatisfiable_smt_quantified_conjunction(self):
        solver = ISLaSolver(
            LANG_GRAMMAR,
            '''
forall <assgn> assgn_1="{<var> var_1} := <rhs>" in <start>:
  var_1 = "a" and
forall <assgn> assgn_2="{<var> var_2} := <rhs>" in <start>:
  var_2 = "b"''',
            activate_unsat_support=True,
        )

        self.assertTrue(
            Exceptional.of(solver.solve)
            .map(lambda _: False)
            .recover(lambda e: isinstance(e, StopIteration))
        )

    def test_unsatisfiable_smt_formulas(self):
        solver = ISLaSolver(
            LANG_GRAMMAR,
            'start = "x := 1"',  # Formula here is just dummy.
            activate_unsat_support=True,
        )

        tree = DerivationTree(
            "<start>",
            (
                DerivationTree(
                    "<stmt>",
                    (
                        DerivationTree(
                            "<assgn>",
                            (
                                DerivationTree("<var>"),
                                DerivationTree(" := ", ()),
                                DerivationTree("<rhs>"),
                            ),
                        ),
                    ),
                ),
            ),
        )
        var_node = tree.get_subtree((0, 0, 0))

        var_1 = language.BoundVariable("var_1", "<var>")
        formula_1 = language.SMTFormula(
            z3_eq(var_1.to_smt(), z3.StringVal("a")),
            instantiated_variables=OrderedSet([var_1]),
            substitutions={var_1: var_node},
        )

        var_2 = language.BoundVariable("var_2", "<var>")
        formula_2 = language.SMTFormula(
            z3_eq(var_2.to_smt(), z3.StringVal("b")),
            instantiated_variables=OrderedSet([var_2]),
            substitutions={var_2: var_node},
        )

        result = solver.eliminate_all_semantic_formulas(
            SolutionState(formula_1 & formula_2, tree)
        )
        self.assertTrue(result.is_present())
        result.if_present(lambda a: self.assertEqual([], a))

    def test_unsatisfiable_forall_exists_formula(self):
        solver = ISLaSolver(
            LANG_GRAMMAR,
            """
forall <assgn> assgn_1:
  exists <assgn> assgn_2:
    before(assgn_2, assgn_1)""",
            activate_unsat_support=True,
        )

        self.assertTrue(
            Exceptional.of(solver.solve)
            .map(lambda _: False)
            .recover(lambda e: isinstance(e, StopIteration))
        )

    def test_unsatisfiable_existential_formula(self):
        tree = DerivationTree(
            "<start>",
            (
                (
                    DerivationTree(
                        "<stmt>",
                        (
                            DerivationTree("<assgn>", id=2),
                            DerivationTree(" ; ", (), id=3),
                            DerivationTree("<stmt>", id=4),
                        ),
                        id=1,
                    )
                ),
            ),
            id=0,
        )

        formula = parse_isla(
            """
forall <assgn> assgn_1:
  exists <assgn> assgn_2:
    before(assgn_2, assgn_1)""",
            LANG_GRAMMAR,
            structural_predicates={BEFORE_PREDICATE},
        ).inner_formula.substitute_expressions(
            {
                start_constant(): tree,
                language.BoundVariable("assgn_1", "<assgn>"): tree.get_subtree((0, 0)),
            }
        )

        solver = ISLaSolver(
            LANG_GRAMMAR,
            """
forall <assgn> assgn_1:
  exists <assgn> assgn_2:
    before(assgn_2, assgn_1)""",
            activate_unsat_support=True,  # This is crucial.
        )

        solver.queue = []
        heapq.heappush(solver.queue, (0, SolutionState(formula, tree)))

        self.assertTrue(
            Exceptional.of(solver.solve)
            .map(lambda _: False)
            .recover(lambda e: isinstance(e, StopIteration))
        )

    def test_implication(self):
        formula = """
not(
  forall <assgn> assgn_1="{<var> var_1} := <rhs>" in start:
      var_1 = "x" implies
  exists <var> var_2 in start:
      var_2 = "x")"""

        solver = ISLaSolver(LANG_GRAMMAR, formula, activate_unsat_support=True)

        self.assertTrue(
            Exceptional.of(solver.solve)
            .map(lambda _: False)
            .recover(lambda e: isinstance(e, StopIteration))
        )

    @pytest.mark.skip("Fails during CI for some reason, never locally")
    def test_equivalent(self):
        f1 = parse_isla('forall <var> var_1 in start: var_1 = "a"')
        f2 = parse_isla('forall <var> var_2 in start: var_2 = "a"')
        self.assertTrue(equivalent(f1, f2, LANG_GRAMMAR, timeout_seconds=60))

    def test_implies(self):
        f1 = parse_isla('forall <var> var_1 in start: var_1 = "a"')
        f2 = parse_isla('exists <var> var_2 in start: var_2 = "a"')
        self.assertTrue(implies(f1, f2, LANG_GRAMMAR, timeout_seconds=60))

    def test_negation_previous_smt_solutions(self):
        # See issue https://github.com/rindPHI/isla/issues/4 --- there should
        # be more than ten solutions to this problem, but before, solutions to
        # all the variables were negated *individually* (not per solution vector)
        # which limited the solution set to ten.
        GRAMMAR: Grammar = {
            "<start>": ["<point>"],
            "<point>": ["<x> <y> <z>"],
            "<x>": ["<digit>"],
            "<y>": ["<digit>"],
            "<z>": ["<digit>"],
            "<digit>": ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"],
        }

        solver = ISLaSolver(
            GRAMMAR,
            """
            forall <point> seed in start:
                (<= (+ (+ (^ (str.to.int seed.<x>) 2) (^ (str.to.int seed.<y>) 2)) (^ (str.to.int seed.<z>) 2)) 900)
            """,
            max_number_smt_instantiations=30,
        )

        solutions = [solver.solve() for _ in range(30)]
        print("\n".join(map(str, solutions)))
        self.assertEqual(30, len(solutions))

    def test_repair_correct_assignment(self):
        formula = """
forall <assgn> assgn_1="<var> := {<var> rhs}" in start:
  exists <assgn> assgn_2="{<var> lhs} := <rhs>" in start:
    (before(assgn_2, assgn_1) and (= lhs rhs))"""

        inp = "x := 1 ; y := x"

        solver = ISLaSolver(LANG_GRAMMAR, formula)
        self.assertEqual(inp, str(solver.repair(inp).orelse(lambda: "").get()))

    def test_repair_wrong_assignment(self):
        formula = """
forall <assgn> assgn_1="<var> := {<var> rhs}" in start:
  exists <assgn> assgn_2="{<var> lhs} := <rhs>" in start:
    (before(assgn_2, assgn_1) and (= lhs rhs))"""
        solver = ISLaSolver(LANG_GRAMMAR, formula)

        self.assertEqual(
            Maybe(True),
            solver.repair("x := 1 ; y := z")
            .map(to_id(print))
            .map(solver.check)
            .orelse(lambda: False),
        )

        self.assertEqual(
            Maybe(True),
            solver.repair("x := 0 ; y := z ; z := c")
            .map(to_id(print))
            .map(solver.check)
            .orelse(lambda: False),
        )

    def test_repair_long_wrong_assignment(self):
        formula = """
forall <assgn> assgn_1="<var> := {<var> rhs}" in start:
  exists <assgn> assgn_2="{<var> lhs} := <rhs>" in start:
    (before(assgn_2, assgn_1) and (= lhs rhs))"""
        solver = ISLaSolver(LANG_GRAMMAR, formula)

        self.assertEqual(
            Maybe(True),
            solver.repair("x := 1 ; x := a ; x := b ; x := c")
            .map(to_id(print))
            .map(solver.check)
            .orelse(lambda: False),
        )

    def test_repair_unrepairable_wrong_assignment(self):
        # If this test fails, it can be a good sign, since it requires a
        # structural change to succeed that was not implemented at the time
        # of writing the test.

        formula = """
forall <assgn> assgn_1="<var> := {<var> rhs}" in start:
  exists <assgn> assgn_2="{<var> lhs} := <rhs>" in start:
    (before(assgn_2, assgn_1) and (= lhs rhs))"""
        solver = ISLaSolver(LANG_GRAMMAR, formula)

        self.assertEqual(
            Maybe(False),
            solver.repair("x := a ; y := z ; z := c")
            .map(solver.check)
            .orelse(lambda: False),
        )

    def test_repair_unbalanced_xml_tree(self):
        solver = ISLaSolver(XML_GRAMMAR, XML_WELLFORMEDNESS_CONSTRAINT)

        self.assertEqual(
            Maybe(True),
            solver.repair("<a>asdf</b>")
            .map(to_id(print))
            .map(solver.check)
            .orelse(lambda: False),
        )

    def test_repair_undeclared_xml_namespace(self):
        solver = ISLaSolver(
            XML_GRAMMAR_WITH_NAMESPACE_PREFIXES, XML_NAMESPACE_CONSTRAINT
        )

        self.assertEqual(
            Maybe(True),
            solver.repair('<a><b x:y="asdf"/></a>')
            .map(to_id(print))
            .map(solver.check)
            .orelse(lambda: False),
        )

        self.assertEqual(
            Maybe(True),
            solver.repair('<a xmlns:z="fdsa"><b x:y="asdf"/></a>')
            .map(to_id(print))
            .map(solver.check)
            .orelse(lambda: False),
        )

        self.assertEqual(
            Maybe(True),
            solver.repair('<a w:z="fdsa"><b x:y="asdf"/></a>')
            .map(to_id(print))
            .map(solver.check)
            .orelse(lambda: False),
        )

    def test_mutate_assignment(self):
        formula = """
forall <assgn> assgn_1="<var> := {<var> rhs}" in start:
  exists <assgn> assgn_2="{<var> lhs} := <rhs>" in start:
    (before(assgn_2, assgn_1) and (= lhs rhs))"""

        inp = DerivationTree.from_parse_tree(
            next(EarleyParser(LANG_GRAMMAR).parse("x := 1 ; y := x"))
        )

        solver = ISLaSolver(LANG_GRAMMAR, formula)
        graph = gg.GrammarGraph.from_grammar(LANG_GRAMMAR)

        for _ in range(10):
            mutated = solver.mutate(inp)
            self.assertTrue(not inp.structurally_equal(mutated))
            self.assertTrue(graph.tree_is_valid(mutated))

    def test_solve_complex_numeric_formula_heartbeat(self):
        heartbeat_request_grammar = {
            "<start>": ["<heartbeat-request>"],
            "<heartbeat-request>": ["\x01<payload-length><payload><padding>"],
            "<payload-length>": ["<byte><byte>"],
            "<payload>": ["<bytes>"],
            "<padding>": ["<bytes>"],
            "<bytes>": ["<byte><bytes>", "<byte>"],
            "<byte>": [chr(i) for i in range(256)],
        }

        length_constraint = """
  256 * str.to_code(<payload-length>.<byte>[1])
+ str.to_code(<payload-length>.<byte>[2]) 
= str.len(<payload>) and
<payload-length>.<byte> = "\x01"
"""

        self.execute_generation_test(
            grammar=heartbeat_request_grammar,
            formula=length_constraint,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=5,
            enforce_unique_trees_in_queue=True,
            num_solutions=5,
            # print_only=True
        )

    def test_solve_complex_quantifier_free_numeric_formula_heartbeat(self):
        heartbeat_request_grammar = {
            "<start>": ["<heartbeat-request>"],
            "<heartbeat-request>": ["\x01<payload-length><payload><padding>"],
            "<payload-length>": ["<byte><byte>"],
            "<payload>": ["<bytes>"],
            "<padding>": ["<bytes>"],
            "<bytes>": ["<byte><bytes>", "<byte>"],
            "<byte>": [chr(i) for i in range(256)],
        }

        solver = ISLaSolver(
            grammar=heartbeat_request_grammar, max_number_smt_instantiations=2
        )

        byte_1 = language.BoundVariable("<byte>_3879", "<byte>")
        payload = language.BoundVariable("<payload>_3824", "<payload>")
        byte_2 = language.BoundVariable("<byte>_3880", "<byte>")

        byte_1_tree = DerivationTree("<byte>", None, id=3879)
        byte_2_tree = DerivationTree("<byte>", None, id=3880)
        payload_tree = DerivationTree("<payload>", None, id=3824)

        formula_1 = language.SMTFormula(
            z3_eq(
                z3.IntVal(256) * z3.StrToCode(byte_1.to_smt())
                + z3.StrToCode(byte_2.to_smt()),
                z3.IntVal(2) * z3.Length(payload.to_smt()),
            ),
            instantiated_variables=OrderedSet([byte_2, payload, byte_1]),
            substitutions={
                byte_1: byte_1_tree,
                byte_2: byte_2_tree,
                payload: payload_tree,
            },
        )

        formula_2 = language.SMTFormula(
            z3_eq(byte_1.to_smt(), z3.StringVal("\x01")),
            instantiated_variables=OrderedSet([byte_1]),
            substitutions={byte_1: byte_1_tree},
        )

        solutions = solver.solve_quantifier_free_formula(
            cast(ImmutableList[language.SMTFormula], (formula_1, formula_2)), 2
        )

        self.assertEqual(2, len(solutions))

        for solution in solutions:
            self.assertEqual(3, len(solution))
            self.assertIn(byte_1_tree, solution)
            self.assertIn(byte_2_tree, solution)
            self.assertIn(payload_tree, solution)

            self.assertEqual(1, ord(str(solution[byte_1_tree])))

            self.assertEqual(
                256 * ord(str(solution[byte_1_tree])) + ord(str(solution[byte_2_tree])),
                2 * len(str(solution[payload_tree])),
            )

    def test_multiple_solutions_heartbeat(self):
        heartbeat_request_grammar = {
            "<start>": ["<heartbeat-request>"],
            "<heartbeat-request>": ["\x01<payload-length><payload><padding>"],
            "<payload-length>": ["<byte><byte>"],
            "<payload>": ["<bytes>"],
            "<padding>": ["<bytes>"],
            "<bytes>": ["<byte><bytes>", "<byte>"],
            "<byte>": [chr(i) for i in range(256)],
        }

        solver = ISLaSolver(
            grammar=heartbeat_request_grammar, max_number_smt_instantiations=2
        )

        byte_1 = language.BoundVariable("<byte>_3879", "<byte>")
        payload = language.BoundVariable("<payload>_3824", "<payload>")
        byte_2 = language.BoundVariable("<byte>_3880", "<byte>")

        byte_1_tree = DerivationTree("<byte>", None, id=3879)
        byte_2_tree = DerivationTree("<byte>", None, id=3880)
        payload_tree = DerivationTree("<payload>", None, id=3824)

        formula_1 = z3_eq(
            z3.IntVal(256) * z3.StrToCode(byte_1.to_smt())
            + z3.StrToCode(byte_2.to_smt()),
            z3.IntVal(2) * z3.Length(payload.to_smt()),
        )

        formula_2 = z3_eq(byte_1.to_smt(), z3.StringVal("\x01"))

        to_exclude = [
            {
                byte_1: z3.StringVal("\x01"),
                byte_2: z3.StringVal("\x00"),
                language.BoundVariable("<payload>_3824", "<payload>"): z3.StringVal(
                    "".join([random.choice(string.printable) for _ in range(256)])
                ),
            }
        ]

        sat_result, model = solver.solve_smt_formulas_with_language_constraints(
            {byte_1, byte_2, payload},
            cast(ImmutableList[z3.BoolRef], (formula_1, formula_2)),
            {
                byte_1: byte_1_tree,
                byte_2: byte_2_tree,
                payload: payload_tree,
            },
            to_exclude,
        )

        self.assertEqual(z3.sat, sat_result)

        self.assertEqual(3, len(model))
        self.assertIn(byte_1, model)
        self.assertIn(byte_2, model)
        self.assertIn(payload, model)

        self.assertEqual(1, ord(str(model[byte_1])))

        self.assertEqual(
            256 * ord(str(model[byte_1])) + ord(str(model[byte_2])),
            2 * len(str(model[payload])),
        )

        self.assertEqual(0, ord(smt_string_val_to_string(to_exclude[0][byte_2])))
        self.assertNotEqual(0, ord(str(model[byte_2])))

        self.assertNotEqual(
            to_exclude[0], {var: z3.StringVal(str(val)) for var, val in model.items()}
        )

    def test_filter_length_variables(self):
        byte_3879 = language.BoundVariable("<byte>_3879", "<byte>")
        payload_3824 = language.BoundVariable("<payload>_3824", "<payload>")
        byte_3880 = language.BoundVariable("<byte>_3880", "<byte>")

        # Test 1: `byte_3879` and `byte_3880` only occur in `str.to.code` expressions
        # or, in the case of `byte_3879`, in a simple equation.

        formula_1 = z3_eq(
            z3.IntVal(256) * z3.StrToCode(byte_3879.to_smt())
            + z3.StrToCode(byte_3880.to_smt()),
            z3.IntVal(2) * z3.Length(payload_3824.to_smt()),
        )

        formula_2 = z3_eq(byte_3879.to_smt(), z3.StringVal("\x01"))

        (length_vars, flexible_vars,) = ISLaSolver.filter_length_variables(
            {byte_3879, payload_3824, byte_3880}, (formula_1, formula_2)
        )

        self.assertEqual({payload_3824}, length_vars)
        self.assertEqual({byte_3879, byte_3880}, flexible_vars)

        # Test 2: Both `byte_...` variables have to be equal. This should not change
        # anything, as we can still work with the codes.

        formula_3 = z3_eq(byte_3879.to_smt(), byte_3880.to_smt())

        (length_vars, flexible_vars,) = ISLaSolver.filter_length_variables(
            {byte_3879, payload_3824, byte_3880}, (formula_1, formula_2, formula_3)
        )

        self.assertEqual({payload_3824}, length_vars)
        self.assertEqual({byte_3879, byte_3880}, flexible_vars)

        # Test 3: Variable `byte_3879` occurs in an equation with the length variable
        # variable `payload_3824`. Thus, all variables end up "flexible."

        formula_4 = z3_eq(byte_3879.to_smt(), payload_3824.to_smt())

        (length_vars, flexible_vars,) = ISLaSolver.filter_length_variables(
            {byte_3879, payload_3824, byte_3880},
            (formula_1, formula_2, formula_4),
        )

        self.assertEqual({byte_3879, byte_3880, payload_3824}, flexible_vars)

    def test_create_fixed_length_tree(self):
        payload_grammar = {
            "<start>": ["<payload>"],
            "<payload>": ["<bytes>"],
            "<bytes>": ["<byte><bytes>", "<byte>"],
            "<byte>": [chr(i) for i in range(256)],
        }

        result = create_fixed_length_tree(
            "<start>", canonical(payload_grammar), target_length=256
        )

        self.assertEqual(256, len(str(result)))

        # Check that parsing works correctly
        parser = PEGParser(payload_grammar)
        parser.parse(str(result))  # No error

    def test_create_zero_length_tree(self):
        payload_grammar = {
            "<start>": ["<payload>"],
            "<payload>": ["<bytes>"],
            "<bytes>": ["", "<byte><bytes>"],
            "<byte>": [chr(i) for i in range(256)],
        }

        result = create_fixed_length_tree(
            "<start>", canonical(payload_grammar), target_length=0
        )

        self.assertEqual(0, len(str(result)))

        # Check that parsing works correctly
        parser = PEGParser(payload_grammar)
        parser.parse(str(result))  # No error

    def test_icmp_payload_bytes_count(self):
        # TODO: If bytes is nullable, `count` does not work!
        grammar = '''
<start> ::= <icmp_message>
<icmp_message> ::= <header> <payload_data>
<header> ::= <type> <code> <checksum> <header_data>
<payload_data> ::= <bytes> | ""
<type> ::= <byte>
<code> ::= <byte>
<checksum> ::= <byte> <byte>
<header_data> ::= <byte> <byte> <byte> <byte>
<byte> ::= <zerof> <zerof> " "
<bytes> ::= <byte> | <byte> <bytes>
<zerof> ::= "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "A" | "B" | "C" | "D" | "E" | "F"'''

        constraint = 'count(<payload_data>, "<byte>", "2")'

        self.execute_generation_test(
            grammar=grammar,
            formula=constraint,
            max_number_free_instantiations=1,
            max_number_smt_instantiations=1,
            enforce_unique_trees_in_queue=True,
            num_solutions=1,
        )

    def test_repair_icmp(self):
        grammar = '''
<start> ::= <icmp_message>
<icmp_message> ::= <header> <payload_data>
<header> ::= <type> <code> <checksum> <header_data>
<payload_data> ::= <bytes> | ""
<type> ::= <byte>
<code> ::= <byte>
<checksum> ::= <byte> <byte>
<header_data> ::= <byte> <byte> <byte> <byte>
<byte> ::= <zerof> <zerof> " "
<bytes> ::= <byte> | <byte> <bytes>
<zerof> ::= "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "A" | "B" | "C" | "D" | "E" | "F"'''

        constraint = '<type> = "08 "'
        inp = "00 00 00 00 00 00 00 00 00 00 "

        solver = ISLaSolver(grammar, constraint)
        result = solver.repair(inp)
        self.assertTrue(result.is_present())
        self.assertEqual("08" + inp[2:], str(result.get()))

    def test_generate_abstracted_trees(self):
        grammar = '''
<start> ::= <icmp_message>
<icmp_message> ::= <header> <payload_data>
<header> ::= <type> <code> <checksum> <header_data>
<payload_data> ::= <bytes> | ""
<type> ::= <byte>
<code> ::= <byte>
<checksum> ::= <byte> <byte>
<header_data> ::= <byte> <byte> <byte> <byte>
<byte> ::= <zerof> <zerof> " "
<bytes> ::= <byte> | <byte> <bytes>
<zerof> ::= "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "A" | "B" | "C" | "D" | "E" | "F"'''

        inp = DerivationTree.from_parse_tree(
            next(
                EarleyParser(parse_bnf(grammar)).parse("00 00 00 00 00 00 00 00 00 00 ")
            )
        )

        abstracted_trees = list(generate_abstracted_trees(inp, {(0, 0, 0)}))
        self.assertTrue(
            any(
                "<type>00 00 00 00 00 00 00 00 00 " == str(tree)
                for tree in abstracted_trees
            )
        )

    def execute_generation_test(
        self,
        formula: language.Formula | str = "true",
        structural_predicates: Set[
            language.StructuralPredicate
        ] = STANDARD_STRUCTURAL_PREDICATES,
        semantic_predicates: Set[
            language.SemanticPredicate
        ] = STANDARD_SEMANTIC_PREDICATES,
        grammar=LANG_GRAMMAR,
        num_solutions=50,
        print_solutions=False,
        max_number_free_instantiations=1,
        max_number_smt_instantiations=1,
        enforce_unique_trees_in_queue=True,
        debug=False,
        state_tree_out="/tmp/state_tree.xml",
        log_out="/tmp/isla_log.txt",
        custom_test_func: Optional[
            Callable[[isla.derivation_tree.DerivationTree], Union[bool, str]]
        ] = None,
        cost_computer: Optional[CostComputer] = None,
        print_only: bool = False,
        timeout_seconds: Optional[int] = None,
        global_fuzzer: bool = False,
        fuzzer_factory: Callable[
            [Grammar], GrammarFuzzer
        ] = lambda grammar: GrammarCoverageFuzzer(grammar),
        tree_insertion_methods=DIRECT_EMBEDDING + SELF_EMBEDDING + CONTEXT_ADDITION,
        activate_unsat_support: bool = False,
    ):
        logger = logging.getLogger(type(self).__name__)

        if debug:
            for f in [f for f in [state_tree_out, log_out] if os.path.exists(f)]:
                os.remove(f)

        solver = ISLaSolver(
            grammar=grammar,
            formula=formula,
            structural_predicates=structural_predicates,
            semantic_predicates=semantic_predicates,
            max_number_free_instantiations=max_number_free_instantiations,
            max_number_smt_instantiations=max_number_smt_instantiations,
            enforce_unique_trees_in_queue=enforce_unique_trees_in_queue,
            debug=debug,
            cost_computer=cost_computer,
            timeout_seconds=timeout_seconds,
            global_fuzzer=global_fuzzer,
            fuzzer_factory=fuzzer_factory,
            tree_insertion_methods=tree_insertion_methods,
            activate_unsat_support=activate_unsat_support,
        )

        if debug:
            file_handler = logging.FileHandler(log_out)
            for name in logging.root.manager.loggerDict:
                logging.getLogger(name).addHandler(file_handler)

        if isinstance(formula, str):
            formula = parse_isla(
                formula, grammar, structural_predicates, semantic_predicates
            )

        constant = Maybe.from_iterator(
            (
                c
                for c in VariablesCollector.collect(formula)
                if isinstance(c, language.Constant) and not c.is_numeric()
            )
        ).orelse(lambda: start_constant())

        def print_tree():
            if debug:
                with open(state_tree_out, "w") as file:
                    file.write(
                        TestSolver.state_tree_to_xml(
                            solver.state_tree_root, solver.state_tree, solver.costs
                        )
                    )
                    print(f"Written derivation data (XML) to {state_tree_out}")
                    print(f"Written log {log_out}")

        solutions_found = 0
        for idx in range(num_solutions):
            try:
                assignment = solver.solve()
            except SolverTimeout:
                logger.info("TIMEOUT")
                break
            except StopIteration:
                logger.info("UNSAT / no more solutions found")
                break

            solutions_found += 1
            logger.info(f"Found solution no. %d: %s", solutions_found, assignment)

            if not print_only:
                self.assertTrue(
                    isla.evaluator.evaluate(
                        formula.substitute_expressions({constant: assignment}),
                        assignment,
                        grammar,
                    ),
                    f"Solution {assignment} does not satisfy constraint {formula}",
                )

                if custom_test_func:
                    test_result = custom_test_func(assignment)
                    if test_result is not True:
                        self.fail(
                            f"Solution WRONG: '{assignment}'"
                            if not isinstance(test_result, str)
                            else f"Solution WRONG: '{assignment}', message: {test_result}, "
                            f"tree: {assignment.to_parse_tree()}"
                        )

            if print_solutions:
                print(str(assignment))

        if not solutions_found:
            self.fail("No solution found.")
        if solutions_found < num_solutions:
            self.fail(f"Only found {solutions_found} solutions")

        print_tree()


if __name__ == "__main__":
    unittest.main()
