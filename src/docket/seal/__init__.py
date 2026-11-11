"""The seal (TD §13.4 to §13.7): cursors, dispositions, ledgers and the journal.

``docket.seal.run.execute_seal`` runs the steps in order. Each step commits a
receipt (``step_receipts``) when it completes, so a seal repeated after a
partial failure never repeats a completed step (FR-APP-06).
"""
