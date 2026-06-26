export PROMPT_TEMPLATE="$(cat << 'EOF'
Perform news summarization (XSUM)
article: {article}

Read the text carefully and provide a concise summary that captures the main points.
Answer:
EOF
)"

python -m tools.prompt_wizard "xsum" \
    --task-type summarization \
    --task-description "summarization of article into single sentence that captures the main point" \
    --dataset-path problems/promptevolve/examples/xsum/dataset/train.csv \
    --target-field target \
    --regexp-pattern "Answer:\s*(.+)" \
    --overwrite
