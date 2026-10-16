"""Signals (TD §8): the rule language, scoring, merge, coverage and traces.

- ``models``: the ``signals`` configuration (rules, references, scoring, budgets);
- ``checks``: its validation with file and line;
- ``engine``: enrichment and rule evaluation;
- ``scoring``: components, the formula, floors, caps and budgets;
- ``merge``: grouping by topic, suppression, coverage, the merge artifact;
- ``stage``: inputs from a run, assess item selection;
- ``replay``: ``docket config test``;
- ``plugins``: plugin predicates and formulas.
"""
