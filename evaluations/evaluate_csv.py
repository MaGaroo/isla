import functools

from grammar_graph.gg import GrammarGraph

from isla.fuzzer import GrammarFuzzer
from isla.performance_evaluator import Evaluator
from isla.solver import ISLaSolver, CostSettings, STD_COST_SETTINGS
from isla_formalizations import csv

max_number_free_instantiations = 10
max_number_smt_instantiations = 2
eval_k = 4

cost_vector = STD_COST_SETTINGS.weight_vectors[0]

g_colno = lambda timeout: ISLaSolver(
    csv.CSV_GRAMMAR,
    csv.CSV_COLNO_PROPERTY,
    max_number_free_instantiations=max_number_free_instantiations,
    max_number_smt_instantiations=max_number_smt_instantiations,
    timeout_seconds=timeout,
    cost_settings=CostSettings((cost_vector,), (1000,), k=eval_k),
    global_fuzzer=False,
    fuzzer_factory=functools.partial(GrammarFuzzer, min_nonterminals=0, max_nonterminals=30),
)

if __name__ == '__main__':
    # logging.basicConfig(level=logging.DEBUG)
    generators = [csv.CSV_GRAMMAR, g_colno]
    jobnames = ["Grammar Fuzzer", "Cnt-Columns"]

    evaluator = Evaluator(
        "CSV",
        generators,
        jobnames,
        csv.csv_lint,
        GrammarGraph.from_grammar(csv.CSV_GRAMMAR),
        default_timeout=60 * 60)

    evaluator.run()