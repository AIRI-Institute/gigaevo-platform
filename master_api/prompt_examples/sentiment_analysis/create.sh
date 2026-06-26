export PROMPT_TEMPLATE="$(cat << 'EOF'
Perform binary sentiment classification of movie review text on positive (1) or negative (0) class
text: {text}

Answer: <0/1>
EOF
)"

python -m tools.prompt_wizard "sentiment_analysis" \
    --task-type classification \
    --task-description "binary sentiment classification of movie review text on positive (1) or negative (0) class" \
    --dataset-path problems/promptevolve/examples/sentiment_analysis/dataset/train.csv \
    --target-field target \
    --regexp-pattern "Answer:\s*([0-9]*\.?[0-9]+)" \
    --overwrite
