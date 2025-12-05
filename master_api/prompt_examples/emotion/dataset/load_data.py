from datasets import load_dataset
import pandas as pd

def create_emotion(data_path='train.csv', test_path='val.csv', n_samples=600, seed=42):
    dataset = load_dataset('dair-ai/emotion')
    val_data = dataset['validation']
    
    emotion_labels = ['sadness', 'joy', 'love', 'anger', 'fear', 'surprise']
    label_to_letter = {0: 'A', 1: 'B', 2: 'C', 3: 'D', 4: 'E', 5: 'F'}
    
    sampled_data = val_data.shuffle(seed=seed).select(range(min(n_samples, len(val_data))))
    
    formatted_data = []
    for item in sampled_data:
        text = item['text']
        label = item['label']
        
        options = (
            f"A. {emotion_labels[0]}\n"
            f"B. {emotion_labels[1]}\n"
            f"C. {emotion_labels[2]}\n"
            f"D. {emotion_labels[3]}\n"
            f"E. {emotion_labels[4]}\n"
            f"F. {emotion_labels[5]}"
        )
        
        formatted_data.append({
            'question': text,
            'options': options,
            'target': label_to_letter[label]
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
    data_df, test_df = create_emotion()
    print(f"\nFirst data example:")
    print(f"Question: {data_df.iloc[0]['question']}")
    print(f"Options:\n{data_df.iloc[0]['options']}")
    print(f"Target: {data_df.iloc[0]['target']}")
    print(f"\nFirst test example:")
    print(f"Question: {test_df.iloc[0]['question']}")
    print(f"Options:\n{test_df.iloc[0]['options']}")
    print(f"Target: {test_df.iloc[0]['target']}")