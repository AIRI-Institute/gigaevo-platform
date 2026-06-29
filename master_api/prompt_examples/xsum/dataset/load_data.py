import pandas as pd
from datasets import load_dataset


def create_xsum(data_path="train.csv", test_path="val.csv", n_samples=600, seed=42):
    """Load and prepare XSum dataset for summarization task."""
    dataset = load_dataset("EdinburghNLP/xsum")
    train_data = dataset["train"]

    sampled_data = train_data.shuffle(seed=seed).select(range(min(n_samples, len(train_data))))

    formatted_data = []
    for item in sampled_data:
        formatted_data.append({"article": item["document"], "target": item["summary"]})

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
    data_df, test_df = create_xsum()
    print(f"\nFirst data example:\n{data_df.iloc[0]['article'][:200]}...")
    print(f"\nTarget: {data_df.iloc[0]['target']}")
