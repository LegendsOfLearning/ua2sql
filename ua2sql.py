import datetime
import gzip
import io
import json
import os
import shutil
import sys
import time

import requests
from requests.auth import HTTPBasicAuth
from sqlalchemy import create_engine, MetaData, Table, Integer, DateTime, String, Column, select, BigInteger, Numeric
from sqlalchemy.dialects import postgresql
from urllib.parse import urlparse, urlunparse

if len(sys.argv) < 2:
    print('please provide path to configuration file. see README.md for specs.')
    exit(1)

CONFIG = {
    'database_url': os.getenv('DATABASE_URL'),
    'unity_project_id': os.getenv('UNITY_PROJECT_ID'),
    'unity_export_api_key': os.getenv('UNITY_API_KEY'),
    'local_collection_path': sys.argv[1],
    'backup_collection_path': os.getenv('UA_BACKUP_COLLECTION_PATH')
}


if not CONFIG['database_url'] \
   or not CONFIG['local_collection_path'] \
   or not CONFIG['unity_project_id'] or not CONFIG['unity_export_api_key']:
    print("missing env variablees. see docs.")
    exit(1)


print('')
print(str(datetime.datetime.now()))
print('*** STARTING COLLECTION / INGESTION JOB ***')


# figure out home directory if necessary
CONFIG['local_collection_path'] = os.path.expanduser(CONFIG['local_collection_path'])
CONFIG['parsed_database_url'] = urlparse(CONFIG['database_url'])
CONFIG['database_url'] = urlunparse(CONFIG['parsed_database_url']._replace(scheme='postgresql+psycopg2'))

print('*** FOR DB [' + CONFIG['parsed_database_url'].path + '] ***')

metadata = MetaData()

job_id_table = Table('JobId', metadata,
                     Column('id', Integer, primary_key=True),
                     Column('ts', DateTime),
                     Column('jobId', String),
                     Column('jobType', String)
                     )

app_start_table = Table('appStart', metadata,
                        Column('id', Integer, primary_key=True),
                        Column('ts', DateTime),
                        Column('submit_time', DateTime),
                        Column('userid', String),
                        Column('remote_ip', postgresql.INET),
                        Column('platform', String),
                        Column('user_agent', String),
                        Column('sdk_ver', String)
                        )

custom_table = Table('custom', metadata,
                     Column('id', Integer, primary_key=True),
                     Column('ts', DateTime),
                     Column('submit_time', DateTime),
                     Column('userid', String),
                     Column('sessionid', BigInteger),
                     Column('remote_ip', postgresql.INET),
                     Column('platform', String),
                     Column('user_agent', String),
                     Column('sdk_ver', String),
                     Column('name', String),
                     Column('custom_params', postgresql.JSONB)
                     )

transaction_table = Table('transaction', metadata,
                          Column('id', Integer, primary_key=True),
                          Column('ts', DateTime),
                          Column('submit_time', DateTime),
                          Column('userid', String),
                          Column('sessionid', BigInteger),
                          Column('remote_ip', postgresql.INET),
                          Column('platform', String),
                          Column('user_agent', String),
                          Column('sdk_ver', String),
                          Column('currency', String),
                          Column('amount', Numeric),
                          Column('transactionid', String),
                          Column('productid', String),
                          Column('receipt', postgresql.JSONB)
                          )

print("starting DB client")
engine = create_engine(CONFIG['database_url'])
conn = engine.connect()

print("building metadata")
metadata.create_all(engine)


# start a request for Unity analytics data and return the ID for the job
def request_raw_analytics_dump(unity_project_id, unity_api_key, start_date, end_date, dump_format, data_set,
                               continue_from=None):
    uri = 'https://analytics.cloud.unity3d.com/api/v2/projects/' + unity_project_id + '/rawdataexports'

    postBodyJson = {'endDate': end_date, 'format': dump_format, 'dataset': data_set}

    if continue_from is not None:
        postBodyJson['continueFrom'] = continue_from
    else:
        postBodyJson['startDate'] = start_date

    headers = {'content-type': 'application/json'}
    r = requests.post(uri, json.dumps(postBodyJson), auth=HTTPBasicAuth(unity_project_id, unity_api_key),
                      headers=headers)

    if r.status_code == 200:
        return r.json()['id']

    return None


# checks whether or not a Unity dump job is done or not
def is_raw_analytics_dump_ready(unity_project_id, unity_api_key, job_id):
    uri = 'https://analytics.cloud.unity3d.com/api/v2/projects/' + unity_project_id + '/rawdataexports/' + job_id
    r = requests.get(uri, auth=HTTPBasicAuth(unity_project_id, unity_api_key))

    if r.status_code == 200:
        return r.json()['status'] == 'completed'

    return False


# extracts and un-compresses all result files in a job
def save_raw_analytics_dump(unity_project_id, unity_api_key, job_id, destination_directory):
    if not os.path.exists(destination_directory):
        os.makedirs(destination_directory)

    uri = 'https://analytics.cloud.unity3d.com/api/v2/projects/' + unity_project_id + '/rawdataexports/' + job_id
    r = requests.get(uri, auth=HTTPBasicAuth(unity_project_id, unity_api_key))

    if r.status_code != 200:
        print('unable to retrieve result due to HTTP error: ' + r.status_code)
        print('URI: ' + uri)
        return

    responseJson = r.json()

    if responseJson['status'] != 'completed':
        print('job status not completed... can\'t dump results yet')
        return

    if 'fileList' not in responseJson['result']:
        print('no files for job: ' + job_id)
        return

    for fileToDownload in responseJson['result']['fileList']:
        fileUri = fileToDownload['url']
        fileName = os.path.splitext(fileToDownload['name'])[0]  # file name w/o extension

        fileRequest = requests.get(fileUri)

        if fileRequest.status_code == 200:
            compressed_file = io.BytesIO(fileRequest.content)
            decompressed_file = gzip.GzipFile(fileobj=compressed_file)

            with open(os.path.join(destination_directory, fileName), 'w+b') as outFile:
                outFile.write(decompressed_file.read())


# returns the last job stored in the database for a job type, if it exists
def find_previous_job_id(job_type):
    s = select([job_id_table]).where(job_id_table.c.jobType == job_type).order_by(job_id_table.c.ts.desc())
    selectResult = conn.execute(s)
    result = selectResult.fetchone()
    selectResult.close()

    if result is None:
        return None

    print('found previous job ' + result['jobId'] + ' for job type ' + job_type)

    return result['jobId']


def to_date_str(unity_timestamp):
    return str(datetime.datetime.strptime(unity_timestamp.split('T')[0], '%Y-%m-%d').date())


def find_current_job_id(unity_project_id, unity_api_key, job_type, end_date):
    uri = 'https://analytics.cloud.unity3d.com/api/v2/projects/' + unity_project_id + '/rawdataexports'
    r = requests.get(uri, auth=HTTPBasicAuth(unity_project_id, unity_api_key))
    if r.status_code != 200:
        return None

    found_jobs = [
        job
        for job in r.json()
        if job['request']['dataset'] == job_type and
           to_date_str(job['request']['endDate']) == end_date
    ]
    found_jobs.sort(key=lambda job: job['createdAt'], reverse=True)

    if len(found_jobs) > 0:
        return found_jobs[0]['id']
    else:
        return None


# removes all files in a directory. does not recurse.
def remove_files_in_directory(path):
    for filename in os.listdir(path):
        full_path = os.path.join(path, filename)
        if os.path.isfile(full_path):
            os.remove(full_path)


# copies raw dump results to a backup folder named after the job type and today's date
def backup_job_results(job_type, local_dump_directory, remote_dump_directory_root):
    src_files = os.listdir(local_dump_directory)

    # don't bother making a backup folder if there are no files for this type
    if len(src_files) == 0:
        return

    # make folders if they aren't there yet
    destination_path = os.path.join(remote_dump_directory_root, job_type, str(datetime.date.today()))
    if not os.path.exists(destination_path):
        os.makedirs(destination_path)

    for file_name in src_files:
        full_file_name = os.path.join(local_dump_directory, file_name)
        shutil.copy2(full_file_name, os.path.join(destination_path, file_name))

    print('backed up raw dump to path: ' + destination_path)


# inserts all rows in a dump into the specified table
# TODO: this isn't very error-safe. if Unity changes their format this will start failing.
def insert_data_into_database(table, dump_directory):
    src_files = os.listdir(dump_directory)

    for file_name in src_files:
        full_file_name = os.path.join(dump_directory, file_name)

        print('ingesting: ' + full_file_name)

        with open(full_file_name) as unityDumpFile:
            totalInsertedRowsCount = 0
            arrayToInsert = []

            for line in unityDumpFile:
                unityJson = json.loads(line)

                dictToInsert = {}

                for c in table.columns:
                    tableColumnName = str(c).split('.')[1]

                    if tableColumnName in unityJson:
                        valToAdd = unityJson[tableColumnName]

                        if tableColumnName == 'ts' or tableColumnName == 'submit_time':
                            valToAdd = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(valToAdd) / 1000))

                        dictToInsert[tableColumnName] = valToAdd

                arrayToInsert.append(dictToInsert)
                if len(arrayToInsert) >= 1000:
                    totalInsertedRowsCount += len(arrayToInsert)
                    conn.execute(table.insert().values(arrayToInsert))
                    print('inserted ' + str(totalInsertedRowsCount) + ' rows.')
                    arrayToInsert = []

            totalInsertedRowsCount += len(arrayToInsert)
            conn.execute(table.insert().values(arrayToInsert))
            print('inserted ' + str(totalInsertedRowsCount) + ' rows.')


# ties it all together - downloads a dump, backs it up, and inserts it into the database
def process_raw_dump(job_type, table, local_dump_directory, remote_dump_directory_root):
    print('collector: starting collection for job: ' + job_type)

    today = datetime.date.today()
    start_date = str(today - datetime.timedelta(days=30))
    end_date = str(today)
    current_job_id = find_current_job_id(CONFIG['unity_project_id'], CONFIG['unity_export_api_key'], job_type, end_date)

    if current_job_id is None:
        continuationJobId = find_previous_job_id(job_type)
        jobId = request_raw_analytics_dump(CONFIG['unity_project_id'],
                                           CONFIG['unity_export_api_key'],
                                           start_date,
                                           end_date,
                                           'json', job_type,
                                           continuationJobId)
        print('started jobId: ' + jobId)
    else:
        jobId = current_job_id
        print('waiting for jobId: ' + jobId)

    while not is_raw_analytics_dump_ready(CONFIG['unity_project_id'],
                                          CONFIG['unity_export_api_key'], jobId):
        time.sleep(5)

    save_raw_analytics_dump(CONFIG['unity_project_id'], CONFIG['unity_export_api_key'], jobId,
                            local_dump_directory)
    print('done! all results for job ' + job_type + ' saved to: ' + local_dump_directory)

    if remote_dump_directory_root is not None:
        backup_job_results(job_type, local_dump_directory, remote_dump_directory_root)

    insert_data_into_database(table, local_dump_directory)
    remove_files_in_directory(local_dump_directory)

    # keep track of the last jobId we ingested for continuation next time
    conn.execute(job_id_table.insert().values(ts=datetime.datetime.utcnow(), jobId=jobId, jobType=job_type))


print("getting rid of any existing file cache in case something failed in the previous run")
remove_files_in_directory(CONFIG['local_collection_path'])

try:
    backup_path = CONFIG['backup_collection_path']
except:
    backup_path = None

process_raw_dump('appStart', app_start_table, CONFIG['local_collection_path'], backup_path)
process_raw_dump('custom', custom_table, CONFIG['local_collection_path'], backup_path)
process_raw_dump('transaction', transaction_table, CONFIG['local_collection_path'], backup_path)

print('*** COMPLETE ***')
