# ua2sql
Python program used to convert Unity Analytics raw data export into rows in PostgreSQL tables.

Usage:

    python ua2sql.py <path to local dump cache>

This program does the following:

1. Collects `appStart`, `custom`, and `transaction` Unity Analytics feeds via Unity's Raw Data Export HTTP API.
2. Connects to a PostgreSQL database and inserts the collected data into database rows.
3. (Optional) Copies collected raw data into a backup location for long term storage. This location is specified via `backup_collection_path`.
4. Deletes the raw dumps stored locally to keep things clean.

To make this friendly with running this in a container, we use environmental variables:

  - `DATABASE_URL` url with database credentials
  - `UNITY_PROJECT_ID` unity project id
  - `UNITY_API_KEY` unity export api key
  - `UA_BACKUP_COLLECTION_PATH` (optional) long term backup path

On the PostgreSQL side this program will create four tables. One table each for `appStart`, `custom`, and `transaction` data streams. These map one-to-one with the data Unity reports. Finally, the program makes a `jobId` table which is used to track the previous job GUID for each data stream type to continue from the last time the program was run.

The first time this program is run it will try to gather as much data from Unity as possible - 30 days. Subsequent runs using the same configuration file will continue exactly where it left off last time. Suggested use is to run this program once per day.

The program has been tested on both Python 2.7.5. and 3.5.2.

Python library dependencies and versions used during development:

1. requests 2.10.0
2. psycopg2 2.6.2
3. SQLAlchemy 1.0.15
