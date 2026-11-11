# Fabric notebook source

# META ********************

# META {
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "a1000000-0000-4000-8000-000000000002",
# META       "default_lakehouse_name": "lh_silver"
# META     }
# META   }
# META }

# MARKDOWN ********************

# Silver orchestration: shared settings, then the silver notebooks as a DAG.
# Customers first; sales lines need the customer keys.

# CELL ********************

%run nb_common

# CELL ********************

DAG = {
    "activities": [
        {
            "name": "silver_customers",
            "path": "nb_silver_customers",
            "timeoutPerCellInSeconds": 900,
            "args": {"env": env},
        },
        {
            "name": "silver_sales",
            "path": "nb_silver_sales",
            "timeoutPerCellInSeconds": 1800,
            "args": {"env": env},
            "dependencies": ["silver_customers"],
            "retry": 1,
        },
    ],
    "concurrency": 2,
}
notebookutils.notebook.validateDAG(DAG)
results = notebookutils.notebook.runMultiple(DAG, {"displayDAGViaGraphviz": False})
