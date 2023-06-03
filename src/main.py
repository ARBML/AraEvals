import pandas as pd
import datasets
import json
import os
from pathlib import Path
from datasets import Value

metrics = {'classification':['accuracy']}

BASE_PATH = "/home/zaid/.evals"

# sys_msg = "Respond only positive or negative sentiment: "
def create_chat_prompt(sys_msg, input_text):
    return [
        {"role": "system", "content": sys_msg}, 
        {"role": "user", "content": input_text}
    ]

def create_chat_example(content, label):
    return [
        {"role": "system", "content": content, "name": "example_user"},
        {"role": "system", "content": label, "name": "example_assistant"},
    ]

def merge_evals(task_class, eval_name, evals_record_paths =[], max_samples = -1, ):
    out_records = []
    curr_sample_id = 0
    curr_event_id = 0 
    for record_path in evals_record_paths:
        with open(record_path, "r") as records_file:
            records_str = records_file.read().splitlines()
            records = [json.loads(record_str) for record_str in records_str]

            #save the spec records for the current json
            spec_records = list(filter(lambda record: "spec" in record.keys(), records)) #extract records with types
            for record in spec_records:
                out_records.append(record)

            records = list(filter(lambda record: "type" in record.keys(), records)) #extract records with types
            task_records = list(filter(lambda record: record["type"] == task_class, records)) #extract records with types
            
            for i in range(len(task_records)):
                # filter the records containing the sample id
                sample_id_records = list(filter(lambda record: record["sample_id"] == f'{eval_name}.test.{i}', records))
                # print(sample_id_records)
                sample_id_record_copy = {}
                for sample_id_record in sample_id_records:
                    sample_id_record_copy = dict(sample_id_record) # don't modify a dict inside a for loop 
                    record_id = int(sample_id_record['sample_id'].split('.')[-1])
                    sample_id_record_copy['sample_id'] = f"{eval_name}.test.{curr_sample_id}"
                    sample_id_record_copy['event_id'] = curr_event_id
                    curr_event_id += 1
                    out_records.append(sample_id_record_copy)
                curr_sample_id += 1

    final_report =  {metric: 0.0 for metric in metrics[task_class]}
    task_metrics = metrics[task_class]
    for record in out_records:
        if 'type' in record and record['type'] == task_class:
            for metric in task_metrics:
                final_report[metric] += record["data"][metric] / max_samples # what happens if there are repeated samples ?:

    out_records.append({'final_report':final_report})
    return out_records

def cast_features(dataset, features= []):
    new_features = dataset.features.copy()
    for feature in features:
        new_features[feature] = Value(dtype='string', id=None)
    dataset = dataset.cast(new_features)
    return dataset

def get_success_record_ids(records_path, task_type = "classification"):
    with open(records_path, "r") as records_file:
        records_str = records_file.read().splitlines()
        records = [json.loads(record_str) for record_str in records_str]
    records = list(filter(lambda record: "type" in record.keys(), records))
    success_records = list(
        filter(
            lambda record: record["type"] == task_type,
            records,
        )
    )
    success_records = sorted(
        success_records,
        key=lambda record: int(record["sample_id"].split(".")[-1]),
    )
    success_ids = [
        int(record["sample_id"].split(".")[-1]) for record in success_records
    ]
    return sorted(set(success_ids))

def pipeline(
    eval_name, 
    task_class,   
    input_column_name,
    target_column_name,
    prompt,
    api_key,
    dataset_name,
    preprocessing_input_fn=None,
    preprocessing_target_fn=None,
    train_split="train",
    test_split=None,
    threads = 1,
    threads_timeout=100,
    max_samples = -1,
    model_name = "gpt-3.5-turbo-0301",
    temperature = 0.0,
    task_description ='',
    num_few_shot=0,
    resume_from_record = False
):

    os.system(f'mkdir -p {BASE_PATH}')
    os.system(f'mkdir -p {BASE_PATH}/evals')
    os.system(f'mkdir -p {BASE_PATH}/eval_results/')
    record_path = f"{BASE_PATH}/eval_results/{eval_name}.jsonl"
    specs_file = f'{BASE_PATH}/evals/{eval_name}.yaml'


    os.system(f'mkdir -p {BASE_PATH}/data/{eval_name}')
    data_path = f'{BASE_PATH}/data/{eval_name}'
    

    train_dataset = datasets.load_dataset(dataset_name, split=train_split)
    if test_split is not None:
        test_dataset = datasets.load_dataset(dataset_name, split=test_split)
    
    if max_samples == -1:
        max_samples = len(test_dataset)
    
    test_dataset = test_dataset.select(range(max_samples))

    if resume_from_record:
        success_ids = get_success_record_ids(records_path=record_path, task_type=task_class.lower())
        unsuccess_ids = set(range(len(test_dataset))) - set(success_ids)
        test_dataset = test_dataset.select(unsuccess_ids)
        if len(test_dataset) == 0:
            raise

        record_path = f"{record_path}_resume"
        


    # Convert the input and target features
    train_dataset = cast_features(train_dataset, features = [input_column_name, target_column_name])
    test_dataset = cast_features(test_dataset, features = [input_column_name, target_column_name])
    
    if preprocessing_input_fn is not None:
        train_dataset = train_dataset.map(preprocessing_input_fn)
        test_dataset = test_dataset.map(preprocessing_input_fn)
    
    if preprocessing_target_fn is not None:
        train_dataset = train_dataset.map(preprocessing_target_fn)
        test_dataset = test_dataset.map(preprocessing_target_fn)
    

    dev_df = train_dataset.to_pandas()
    dev_df["sample"] = dev_df.apply(lambda x: create_chat_example(x[input_column_name], x[target_column_name]), axis=1)
    dev_df[["sample"]].to_json(f'{data_path}/few_shot.jsonl', lines=True, orient="records",force_ascii=False)

    test_df = test_dataset.to_pandas()
    test_df["input"] = test_df[input_column_name].apply(lambda x: create_chat_prompt(prompt, x))
    test_df["ideal"] = test_df[target_column_name]
    test_df[["input", "ideal"]].to_json(f'{data_path}/samples.jsonl', lines=True, orient="records",force_ascii=False)

    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["EVALS_THREADS"]=f"{threads}"
    os.environ["EVALS_THREAD_TIMEOUT"]=f"{threads_timeout}"
    
    specs = f"""
{eval_name}:
    id: {eval_name}.test.v1
    description: {task_description}
{eval_name}.test.v1:
    class: evals.elsuite.{task_class.lower()}:{task_class.lower().title()}
    args:
        samples_jsonl: {BASE_PATH}/data/{eval_name}/samples.jsonl
        num_few_shot: {num_few_shot} # max few shots to use
        """.strip()


    with open(specs_file, "w") as file:
        file.write(specs)

    
    os.system(f"oaieval {model_name} {eval_name}\
                --seed 41 \
                --modelspec_extra_options temperature={temperature} --max_samples {max_samples} --record_path {record_path}")

    if resume_from_record:
        eval_paths = [record_path.split('resume')[0][:-1], record_path]
        print('merge evals')
        records = merge_evals(task_class=task_class, eval_name=eval_name, max_samples= max_samples, evals_record_paths=eval_paths)
        with open(f'{BASE_PATH}/eval_results/{eval_name}_full.jsonl', 'w') as f:
            for sample in records:
                f.write(json.dumps(sample) + "\n")

def map_labels(sample):
    if sample["label"] == "1":
        sample["label"] = "Positive"
    else:
        sample["label"] = "Negative"
    return sample

pipeline(
    eval_name = "ajgt2",
    task_class= "classification",
    task_description = "Arabic text classification",
    input_column_name = 'text',
    target_column_name = 'label',
    prompt='What is the sentiment?',
    api_key='<api-key>',
    dataset_name="ajgt_twitter_ar",
    preprocessing_input_fn=None,
    preprocessing_target_fn=map_labels,
    train_split="train",
    test_split="train",
    threads = 1,
    threads_timeout=100,
    model_name = "gpt-3.5-turbo-0301",
    temperature = 0.0,
    resume_from_record = True,
    max_samples= 3
)