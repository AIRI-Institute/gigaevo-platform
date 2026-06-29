import pandas as pd
from datasets import load_dataset


def create_imdb(data_path="train.csv", test_path="val.csv", n_samples=600, seed=42):
    dataset = load_dataset("stanfordnlp/imdb")
    val_data = dataset["test"]

    sampled_data = val_data.shuffle(seed=seed).select(range(min(n_samples, len(val_data))))

    df = pd.DataFrame({"text": sampled_data["text"], "target": sampled_data["label"]})

    n_data = int(0.5 * len(df))
    data_df = df.iloc[:n_data].reset_index(drop=True)
    test_df = df.iloc[n_data:].reset_index(drop=True)

    data_df.to_csv(data_path, index=False)
    test_df.to_csv(test_path, index=False)

    print(f"Created {data_path} with {len(data_df)} rows")
    print(f"Created {test_path} with {len(test_df)} rows")

    return data_df, test_df


if __name__ == "__main__":
    # Example usage
    data_df, test_df = create_imdb()
    print(f"\nFirst data example:\nText: {data_df.iloc[0]['text'][:100]}...\nTarget: {data_df.iloc[0]['target']}")
    print(f"\nFirst test example:\nText: {test_df.iloc[0]['text'][:100]}...\nTarget: {test_df.iloc[0]['target']}")
