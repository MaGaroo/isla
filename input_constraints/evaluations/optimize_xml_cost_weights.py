import logging
import random
import xml.etree.ElementTree as ET

from input_constraints import isla
from input_constraints.evaluator import auto_tune_weight_vector
from input_constraints.tests.subject_languages.xml_lang import XML_NAMESPACE_CONSTRAINT, XML_WELLFORMEDNESS_CONSTRAINT, \
    XML_GRAMMAR_WITH_NAMESPACE_PREFIXES


def validate_xml(inp: isla.DerivationTree) -> bool:
    try:
        ET.fromstring(str(inp))
        return True
    except ET.ParseError:
        return False


if __name__ == '__main__':
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("evaluator").setLevel(logging.DEBUG)

    random.seed(654683513684651)

    tune_result = auto_tune_weight_vector(
        XML_GRAMMAR_WITH_NAMESPACE_PREFIXES,
        XML_WELLFORMEDNESS_CONSTRAINT & XML_NAMESPACE_CONSTRAINT,
        validator=validate_xml,
        timeout=45,  # How long should a single configuration be evaluated
        population_size=80,  # How many configurations should be produced at the beginning
        generations=5,  # Evolutionary tuning: How many generations should I produce using crossover / mutation
        cpu_count=-1  # Run in parallel: Use all cores (cpu_count == 1 implies single-threaded)
    )

    print(f"Optimal cost vector: {tune_result[1]}")