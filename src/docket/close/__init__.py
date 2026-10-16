"""Close (TD §13.4 to §13.7): cursors, dispositions, ledgers, the journal and hooks.

``docket.close.run.execute_close`` runs the steps in order. Each step commits a
receipt (``step_receipts``) when it completes, so a close resumed after a
partial failure never repeats a completed step (FR-APP-06).
"""
