import re

import pandas as pd
from datasets import load_dataset


def create_gsm8k(data_path="train.csv", test_path="val.csv", n_samples=600, seed=42):
    dataset = load_dataset("openai/gsm8k", "main")
    val_data = dataset["test"]

    sampled_data = val_data.shuffle(seed=seed).select(range(min(n_samples, len(val_data))))

    regex_pattern = r"#### (\-?[0-9\.\,]+)"

    formatted_data = []
    for item in sampled_data:
        question = f"{item['question']}"

        match = re.search(regex_pattern, item["answer"])
        if match:
            target = match.group(1)
        else:
            continue

        formatted_data.append({"problem": question, "target": target})

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
    data_df, test_df = create_gsm8k()
    print(f"\nFirst data example:\n{data_df.iloc[0]['problem']}\nTarget: {data_df.iloc[0]['target']}")
    print(f"\nFirst test example:\n{test_df.iloc[0]['problem']}\nTarget: {test_df.iloc[0]['target']}")
