#!/usr/bin/env python

import MySQLdb
from google.cloud import bigquery
import logging
import os
from MySQLdb.converters import conversions
import click
import MySQLdb.cursors
from google.cloud.exceptions import ServiceUnavailable
import threading
import multiprocessing as mp

bqTypeDict = { 'int' : 'INTEGER',
               'varchar' : 'STRING',
               'double' : 'FLOAT',
               'tinyint' : 'INTEGER',
               'decimal' : 'FLOAT',
               'text' : 'STRING',
               'smallint' : 'INTEGER',
               'char' : 'STRING',
               'bigint' : 'INTEGER',
               'float' : 'FLOAT',
               'longtext' : 'STRING',
               'datetime' : 'TIMESTAMP'
              }

def conv_date_to_timestamp(str_date):
    import time
    import datetime

    date_time = MySQLdb.times.DateTime_or_None(str_date)
    if date_time is None:
      return 0
    unix_timestamp = (date_time - datetime.datetime(1970,1,1)).total_seconds()

    return unix_timestamp

def Connect(host, database, user, password):
    ## fix conversion. datetime as str and not datetime object
    conv=conversions.copy()
    conv[12]=conv_date_to_timestamp
    return MySQLdb.connect(host=host, db=database, user=user, passwd=password,
        conv=conv, cursorclass=MySQLdb.cursors.SSCursor, charset='utf8', use_unicode=True)


def BuildSchema(host, database, user, password, table):
    logging.debug('build schema for table %s in database %s' % (table, database))
    conn = Connect(host, database, user, password)
    cursor = conn.cursor()
    cursor.execute("DESCRIBE %s;" % table)

    tableDecorator = cursor.fetchall()
    schema = []

    for col in tableDecorator:
        colType = col[1].split("(")[0]
        if colType not in bqTypeDict:
            logging.warning("Unknown type detected, using string: %s", str(col[1]))

        field_mode = "NULLABLE" if col[2] == "YES" else "REQUIRED"
        field = bigquery.SchemaField(col[0], bqTypeDict.get(colType, "STRING"), mode=field_mode)

        schema.append(field)

    return tuple(schema)


def bq_load(table, data, max_retries=5):
    logging.info("Sending request")
    uploaded_successfully = False
    num_tries = 0

    while not uploaded_successfully and num_tries < max_retries:
        try:
            insertResponse = table.insert_data(data)

            for row in insertResponse:
                if 'errors' in row:
                    logging.error('not able to upload data: %s', row['errors'])

            uploaded_successfully = True
            logging.info("Values uploaded")
        except ServiceUnavailable as e:
            num_tries += 1
            logging.error('insert failed with exception trying again retry %d', num_tries )
        except Exception as e:
            num_tries += 1
            logging.error('not able to upload data: %s', str(e) )


@click.command()
@click.option('-h', '--host', default='127.0.0.1', help='MySQL hostname')
@click.option('-d', '--database', required=True, help='MySQL database')
@click.option('-u', '--user', default='root', help='MySQL user')
@click.option('-p', '--password', default='', help='MySQL password')
@click.option('-t', '--table', required=True, help='MySQL table')
@click.option('-i', '--projectid', required=True, help='Google BigQuery Project ID')
@click.option('-n', '--dataset', required=True, help='Google BigQuery Dataset name')
@click.option('-l', '--limit',  default=0, help='max num of rows to load')
@click.option('-s', '--batch_size',  default=1000, help='max num of rows to load')
@click.option('-k', '--key',  default='google_key.json', help='Location of google service account key (relative to current working dir)')
@click.option('-v', '--verbose',  default=0, count=True, help='verbose')
@click.option('-dt','--delete_table', default=0, count=True, help='Delete existing table on BQ')
def SQLToBQBatch(host, database, user, password, table, projectid, dataset, limit, batch_size, key, verbose, delete_table):
    # set to max verbose level
    verbose = verbose if verbose < 3 else 3
    loglevel = logging.ERROR - (10 * verbose)

    logging.basicConfig(level=loglevel)

    logging.info("Starting SQLToBQBatch. Got: Table: %s, Limit: %i", table, limit)

    ## set env key to authenticate application
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = "%s/%s" % (os.getcwd(), key)

    # Instantiates a client
    bigquery_client = bigquery.Client()

    try:
        bq_dataset = bigquery_client.dataset(dataset)

        # Creates the new dataset
        bq_dataset.create()
        logging.info("Added Dataset")
    except Exception as e:
        if ("Already Exists: " in str(e)):
            logging.info("Dataset already exists")
        else:
            logging.error("Error creating dataset: %s Error", str(e))

    try:
        bq_table = bq_dataset.table(table)
        if delete_table>0:
          logging.info('Trying to delete table %s',table)
          try:
            bq_table.delete()
            logging.info('Table %s deleted',table)
          except Exception as e:
            logging.info('Table %s didnt exist',table)
        bq_table.schema = BuildSchema(host, database, user, password, table)
        bq_table.create()

        logging.info("Added Table %s", table)
    except Exception as e:
        logging.info(e)
        if ("Already Exists: " in str(e)):
            logging.info("Table %s already exists", table)
        else:
            logging.error("Error creating table %s: %s Error", table, str(e))

    conn = Connect(host, database, user, password)
    cursor = conn.cursor()

    logging.info("Starting load loop")
    cursor.execute("SELECT * FROM %s" % (table))

    cur_batch = []
    count = 0
    pool = mp.Pool(mp.cpu_count())
    logging.info('CPUs: %i',mp.cpu_count())
    for row in cursor:
        count += 1

        if limit != 0 and count >= limit:
            logging.info("limit of %d rows reached", limit)
            break

        cur_batch.append(row)

        if count % batch_size == 0 and count != 0:
            logging.info('Pooling %i',count)
            th = pool.apply(bq_load, args=(bq_table,cur_batch ))
            
            #bq_load(bq_table, cur_batch)

            cur_batch = []
            logging.info("Threaded %i rows", count)

    # send last elements
    bq_load(bq_table, cur_batch)
    logging.info("Finished (%i total)", count)
    pool.close()


if __name__ == '__main__':
    ## run the command
    SQLToBQBatch()
