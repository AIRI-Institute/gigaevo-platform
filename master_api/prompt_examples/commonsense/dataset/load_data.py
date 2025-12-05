from datasets import load_dataset
import pandas as pd

def create_commonsense_qa(data_path='train.csv', test_path='val.csv', n_samples=600, seed: int = 42):
    dataset = load_dataset('tau/commonsense_qa')
    val_data = dataset['validation']

    val_data = val_data.shuffle(seed=seed).select(range(min(n_samples, len(val_data))))

    formatted_data = []
    for item in val_data:
        question_text = item['question'].strip()
        choices = item['choices']

        options = (
            f"A. {choices['text'][0]}\n"
            f"B. {choices['text'][1]}\n"
            f"C. {choices['text'][2]}\n"
            f"D. {choices['text'][3]}\n"
            f"E. {choices['text'][4]}"
        )
        
        formatted_data.append({
            'question': question_text,
            "options": options,
            'target': item['answerKey']
        })
    
    df = pd.DataFrame(formatted_data)
    
    n_data = int(0.5 * len(df))
    data_df = df.iloc[:n_data].reset_index(drop=True)
    test_df = df.iloc[n_data:].reset_index(drop=True)
    
    data_df.to_csv(data_path, index=False)
    test_df.to_csv(test_path, index=False)
    
    print(f"Created {data_path} with {len(data_df)} rows")
    print(f"Created {test_path} with {len(test_df)} rows")
    
    return data_df, test_df


if __name__ == "__main__":
    data_df, test_df = create_commonsense_qa()
    print(f"\nFirst data example:")
    print(f"Question: {data_df.iloc[0]['question']}")
    print(f"Options:\n{data_df.iloc[0]['options']}")
    print(f"Target: {data_df.iloc[0]['target']}")
    print(f"\nFirst test example:")
    print(f"Question: {test_df.iloc[0]['question']}")
    print(f"Options:\n{test_df.iloc[0]['options']}")
    print(f"Target: {test_df.iloc[0]['target']}")