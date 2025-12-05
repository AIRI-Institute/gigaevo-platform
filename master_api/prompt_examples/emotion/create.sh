export PROMPT_TEMPLATE="$(cat << 'EOF'
Perform emotion classification by selecting the correct emotion (sadness, joy, love, anger, fear, or surprise) from text
question: {question}
options: {options}

Output your final answer in the format:
Answer: <letter>
EOF
)"

python -m tools.prompt_wizard "emotion" \
    --task-type multi_choice \
    --task-description "emotion classification by selecting the correct emotion (sadness, joy, love, anger, fear, or surprise) from text" \
    --dataset-path problems/promptevolve/examples/emotion/dataset/train.csv \
    --target-field target \
    --overwrite
