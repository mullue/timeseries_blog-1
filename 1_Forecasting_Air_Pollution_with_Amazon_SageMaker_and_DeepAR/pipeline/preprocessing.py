import argparse
import os
import warnings

import boto3, time, json, warnings, os, re
import urllib.request
from datetime import date, timedelta
import numpy as np
import pandas as pd
import geopandas as gpd
from multiprocessing import Pool

# the train test split date is used to split each time series into train and test sets
train_test_split_date = date.today() - timedelta(days = 30)

# the sampling frequency determines the number of hours per sample
# and is used for aggregating and filling missing values
frequency = '1'

warnings.filterwarnings('ignore')


def get_athena_s3_staging_dir():
    session = boto3.Session()
    account_id = session.client('sts').get_caller_identity().get('Account')
    return f's3://{account_id}-openaq-forecasting/athena/results/'
    
def athena_create_table(query_file, wait=None):
    create_table_uri = athena_execute(query_file, 'txt', wait)
    return create_table_uri
    
def athena_query_table(query_file, wait=None):
    results_uri = athena_execute(query_file, 'csv', wait)
    return results_uri

def athena_execute(query_file, ext, wait):
    with open(query_file) as f:
        query_str = f.read()  
        
    athena = boto3.client('athena')
    s3_dest = get_athena_s3_staging_dir()
    query_id = athena.start_query_execution(
        QueryString= query_str, 
         ResultConfiguration={'OutputLocation': s3_dest}
    )['QueryExecutionId']
        
    results_uri = f'{s3_dest}{query_id}.{ext}'
        
    start = time.time()
    while wait == None or wait == 0 or time.time() - start < wait:
        result = athena.get_query_execution(QueryExecutionId=query_id)
        status = result['QueryExecution']['Status']['State']
        if wait == 0 or status == 'SUCCEEDED':
            break
        elif status in ['QUEUED','RUNNING']:
            continue
        else:
            raise Exception(f'query {query_id} failed with status {status}')

            time.sleep(3) 

    return results_uri       

def map_s3_bucket_path(s3_uri):
    pattern = re.compile('^s3://([^/]+)/(.*?([^/]+)/?)$')
    value = pattern.match(s3_uri)
    return value.group(1), value.group(2)

def get_sydney_openaq_data(sql_query_file_path = "/opt/ml/processing/sql/sydney.dml"):
    query_results_uri = athena_query_table(sql_query_file_path)
    print (f'reading {query_results_uri}')
    bucket_name, key = map_s3_bucket_path(query_results_uri)
    print(f'bucket: {bucket_name}; with key: {key}')
    local_result_file = 'result.csv'
    s3 = boto3.resource('s3')
    s3.Bucket(bucket_name).download_file(key, local_result_file)
    raw = pd.read_csv(local_result_file, parse_dates=['timestamp'])

    return raw

def featurize(raw):

    def fill_missing_hours(df):
        df = df.reset_index(level=categorical_levels, drop=True)                                    
        index = pd.date_range(df.index.min(), df.index.max(), freq='1H')
        return df.reindex(pd.Index(index, name='timestamp'))

    # Sort and index by location and time
    categorical_levels = ['country', 'city', 'location', 'parameter']
    index_levels = categorical_levels + ['timestamp']
    indexed = raw.sort_values(index_levels, ascending=True)
    indexed = indexed.set_index(index_levels)
    # indexed.head()    
    
    # Downsample to hourly samples by maximum value
    downsampled = indexed.groupby(categorical_levels + [pd.Grouper(level='timestamp', freq='1H')]).max()

    # Back fill missing values
    filled = downsampled.groupby(level=categorical_levels).apply(fill_missing_hours)
    filled[filled['value'].isnull()].groupby('location').count().describe()
    
    filled['value'] = filled['value'].interpolate().round(2)
    filled['point_latitude'] = filled['point_latitude'].fillna(method='pad')
    filled['point_longitude'] = filled['point_longitude'].fillna(method='pad')

    # Create Features
    aggregated = filled.reset_index(level=4)\
        .groupby(level=categorical_levels)\
        .agg(dict(timestamp='first', value=list, point_latitude='first', point_longitude='first'))\
        .rename(columns=dict(timestamp='start', value='target'))    
    aggregated['id'] = np.arange(len(aggregated))
    aggregated.reset_index(inplace=True)
    aggregated.set_index(['id']+categorical_levels, inplace=True)
    
    metadata = gpd.GeoDataFrame(
        aggregated.drop(columns=['target','start']), 
        geometry=gpd.points_from_xy(aggregated.point_longitude, aggregated.point_latitude), 
        crs={"init":"EPSG:4326"}
    )
    metadata.drop(columns=['point_longitude', 'point_latitude'], inplace=True)
    # set geometry index
    metadata.set_geometry('geometry')

    # Add Categorical features
    level_ids = [level+'_id' for level in categorical_levels]
    for l in level_ids:
        aggregated[l], index = pd.factorize(aggregated.index.get_level_values(l[:-3]))

    aggregated['cat'] = aggregated.apply(lambda columns: [columns[l] for l in level_ids], axis=1)
    features = aggregated.drop(columns=level_ids+ ['point_longitude', 'point_latitude'])
    features.reset_index(level=categorical_levels, inplace=True, drop=True)
    
    return features

def filter_dates(df, min_time, max_time, frequency):
    min_time = None if min_time is None else pd.to_datetime(min_time)
    max_time = None if max_time is None else pd.to_datetime(max_time)
    interval = pd.Timedelta(frequency)
    
    def _filter_dates(r): 
        if min_time is not None and r['start'] < min_time:
            start_idx = int((min_time - r['start']) / interval)
            r['target'] = r['target'][start_idx:]
            r['start'] = min_time
        
        end_time = r['start'] + len(r['target']) * interval
        if max_time is not None and end_time > max_time:
            end_idx = int((end_time - max_time) / interval)
            r['target'] = r['target'][:-end_idx]
            
        return r
    
    filtered = df.apply(_filter_dates, axis=1) 
    filtered = filtered[filtered['target'].str.len() > 0]
    return filtered

def split_train_test_data(features, days = 30):
    train_test_split_date = date.today() - timedelta(days = days)
    train = filter_dates(features, None, train_test_split_date, '1H')
    test = filter_dates(features, train_test_split_date, None, '1H')
    return train, test

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--split-days", type=int, default=30)
    parser.add_argument("--region", type=str, default='us-east-1')
    args, _ = parser.parse_known_args()

    print("Received arguments {}".format(args))
    split_days = args.split_days
    region = args.region
    
    # definte environment variable
    os.environ['AWS_DEFAULT_REGION'] = region

    # Create OpenAQ Athena table
    athena_create_table('/opt/ml/processing/sql/openaq.ddl')
    
    # Query Sydney OpenAQ data
    raw = get_sydney_openaq_data()
    features = featurize(raw)
    train, test = split_train_test_data(features)

    all_features_output_path = os.path.join(
        "/opt/ml/processing/output/all", "all_features.json"
    )
    print("Saving all features to {}".format(all_features_output_path))
    features.to_json(all_features_output_path, orient='records', lines = True)
    
    train_features_output_path = os.path.join(
        "/opt/ml/processing/output/train", "train.json"
    )
    print("Saving train features to {}".format(train_features_output_path))
    train.to_json(train_features_output_path, orient='records', lines = True)

    test_features_output_path = os.path.join(
        "/opt/ml/processing/output/test", "test.json"
    )
    print("Saving test features to {}".format(test_features_output_path))
    test.to_json(test_features_output_path, orient='records', lines = True)