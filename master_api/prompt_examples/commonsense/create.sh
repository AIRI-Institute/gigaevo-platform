export PROMPT_TEMPLATE="$(cat << 'EOF'
Perform commonsense reasoning by selecting the correct multiple-choice answer (A-E) based on question
question: {question}
options: {options}

Output your final answer in the format:
Answer: <letter>
EOF
)"

python -m tools.prompt_wizard "commonsense" \
    --task-type multi_choice \
    --task-description "commonsense reasoning by selecting the correct multiple-choice answer (A-E) based on question" \
    --dataset-path problems/promptevolve/examples/commonsense/dataset/train.csv \
    --target-field target \
    --overwrite
