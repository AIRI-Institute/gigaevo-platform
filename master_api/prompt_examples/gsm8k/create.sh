export PROMPT_TEMPLATE="$(cat << 'EOF'
Perform math word problem solving (GSM8K)
problem: {problem}

Please reason step by step, then provide your final answer in the format:
Answer: <your numerical answer>
EOF
)"

python -m tools.prompt_wizard "gsm8k" \
    --task-type math \
    --task-description "math word problem solving by computing the correct numerical answer" \
    --dataset-path problems/promptevolve/examples/gsm8k/dataset/train.csv \
    --target-field target \
    --overwrite
