"""
FabriBuddy Report Audience — who can see which Power BI report, who opens it, and
which paid seats nobody uses.

The second product in this app. It shares the platform (accounts, the
connected tenant and its SPN, scope, schedule, plans, the console) with
Lineage and keeps its own scanners (`powerbi/`), scan runs and audit-log store.
Metadata and audit events only — never report data.
"""
