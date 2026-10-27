# fabribuddy-core

The Python package behind [FabriBuddy](https://zan-tavcar.github.io/msfabric-fabribuddy/): the
scanner and page logic of the Data Catalog and Report Audience, run from a Fabric notebook. You
don't install it yourself: `Deploy_FabriBuddy` stores it in your `FabriBuddy_DB`, and
`FabriBuddy_Scan` installs it from there.

- `src/fabribuddy_core`: the scanner and page logic.
- `src/fabribuddy_intenant`: the in-tenant runner: the SQL database, and the pages the app reads.
