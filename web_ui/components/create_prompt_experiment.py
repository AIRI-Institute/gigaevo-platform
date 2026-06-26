"""Create Prompt Experiment tab component for LLM prompt evolution."""

import os
import re
import tempfile
from typing import Optional

import gradio as gr
import requests
from config.settings import (
    INTERNAL_S3_API_URL,
    S3_API_URL,
    STORAGE_BUCKET_NAME,
    VALIDATION_RULES,
)
from loguru import logger
from utils.file_handlers import (
    cleanup_temp_file,
    count_csv_rows,
    extract_source_path_from_upload,
    read_csv_columns,
)
from utils.validators import (
    get_default_target_choices,
    validate_experiment_name,
    validate_file_upload,
    validate_max_iterations,
    validate_regexp_pattern,
)

from common.llm_registry import (
    get_default_llm_model_id,
    get_default_prompt_llm_model_id,
    get_llm_model_choices,
)

from .base import BaseComponent


class CreatePromptExperimentComponent(BaseComponent):
    """Component for creating new prompt-based experiments."""

    def __init__(self, *args, **kwargs):
        """Initialize create prompt experiment component."""
        super().__init__(*args, **kwargs)
        self.bucket_name = None

    def build(self) -> gr.Column:
        """Build the create prompt experiment tab.

        Returns:
            Gradio Column component
        """
        with gr.Column() as component:
            gr.Markdown("## Create Prompt Experiment")
            gr.Markdown("#### Start with a Preset Example")
            with gr.Row():
                btn_cmns = gr.Button("commonsense", size="sm")
                btn_emot = gr.Button("emotion", size="sm")
                btn_gsm8k = gr.Button("gsm8k", size="sm")
                btn_sent = gr.Button("sentiment_analysis", size="sm")
                btn_xsum = gr.Button("xsum", size="sm")

            with gr.Row():
                with gr.Column(scale=1):
                    name_input = gr.Textbox(
                        label="Experiment Name",
                        placeholder="Enter experiment name...",
                    )
                    description_input = gr.Textbox(
                        label="Description",
                        placeholder="Optional description of your prompt experiment...",
                        lines=3,
                    )

                with gr.Column(scale=1):
                    data_file_input = gr.File(
                        label="Data File",
                        file_types=[".csv", ".json", ".txt"],
                        height=100,
                        elem_classes="short-upload",
                    )
                    dataset_info = gr.Textbox(
                        label="Dataset Source",
                        value="No dataset selected",
                        interactive=False,
                        info="Current dataset being used for the experiment",
                    )
                    target_field_input = gr.Dropdown(
                        label="Target Column",
                        choices=[],
                        interactive=True,
                        value=None,
                        info="Select the column you want to predict",
                    )

                with gr.Column(scale=1):
                    max_iterations_input = gr.Slider(
                        minimum=VALIDATION_RULES["max_iterations"]["min"],
                        maximum=VALIDATION_RULES["max_iterations"]["max"],
                        value=100,
                        label="Max Iterations",
                        info="Maximum number of evolution iterations",
                    )
                    llm_model_input = gr.Dropdown(
                        choices=get_llm_model_choices(),
                        value=get_default_llm_model_id(),
                        label="Evolution Model",
                        info="Model used by the evolution engine",
                    )
                    prompt_llm_model_input = gr.Dropdown(
                        choices=get_llm_model_choices(),
                        value=get_default_prompt_llm_model_id(),
                        label="Prompt Template Model",
                        info="Model used by the prompt template evaluator",
                    )
                    dataset_size_input = gr.Number(
                        value=None,
                        label="Dataset Size (rows)",
                        info="Number of rows to use from dataset (min 10% of dataset, max 100%, leave empty to use all)",
                        precision=0,
                    )
                    test_size_input = gr.Slider(
                        minimum=0.1,
                        maximum=0.9,
                        value=0.2,
                        step=0.05,
                        label="Test Size Ratio",
                        info="Fraction of dataset to use for testing (0.2 = 20%)",
                    )
                    split_info = gr.Markdown(
                        value="",
                        visible=False,
                        elem_classes=["split-info"],
                    )

            with gr.Row():
                with gr.Column(scale=2):
                    gr.Markdown("### Prompt Template")

                    with gr.Accordion("📝 Prompt Template Examples", open=False):
                        gr.Markdown(
                            """
                            **Template Format:**
                            ```
                            Your task: [describe what you want the LLM to do]

                            Context: {column1}, {column2}, {column3}
                            Question: What is the *target*?

                            Answer: [expected answer]
                            ```

                            **Example:**
                            ```
                            You are a sentiment analyst. Classify the review as positive or negative.

                            Review text: {review_text}
                            Rating: {rating}
                            Category: {product_category}

                            Answer: [expected answer]
                            ```
                            """
                        )

                    base_prompt_input = gr.Textbox(
                        label="Base Prompt",
                        placeholder="Enter your prompt template with {column_name} placeholders...",
                        lines=6,
                        info="Use {column_name} syntax for template placeholders. Example: 'Predict the target based on {feature1} and {feature2}'",
                    )

                with gr.Column(scale=1):
                    gr.Markdown("#### Available Columns (click to add to prompt)")
                    column_buttons_container = gr.Group()
                    with column_buttons_container:
                        column_buttons_row = gr.Row(visible=False, elem_classes=["column-buttons-row"])
                        with column_buttons_row:
                            col_btn_1 = gr.Button("", size="sm", visible=False)
                            col_btn_2 = gr.Button("", size="sm", visible=False)
                            col_btn_3 = gr.Button("", size="sm", visible=False)
                            col_btn_4 = gr.Button("", size="sm", visible=False)
                            col_btn_5 = gr.Button("", size="sm", visible=False)
                            col_btn_6 = gr.Button("", size="sm", visible=False)
                            col_btn_7 = gr.Button("", size="sm", visible=False)
                            col_btn_8 = gr.Button("", size="sm", visible=False)

                    no_columns_msg = gr.Markdown("Upload a data file to see available columns", visible=True)
                    generate_baseline_btn = gr.Button(
                        "Generate Baseline Prompt", size="sm", variant="secondary", visible=False
                    )

                    gr.Markdown("#### 🔧 Validation Criteria")
                    with gr.Group():
                        validation_type_input = gr.Dropdown(
                            choices=["Binary (0/1)", "Continuous (0..1)"],
                            label="Validation Type",
                            info="Select how the target column should be validated",
                            interactive=True,
                        )

                        with gr.Group() as binary_validation_group:
                            binary_validation_method_input = gr.Dropdown(
                                choices=["equality", "substring", "regexp"],
                                label="Binary Validation Method",
                                info="Method for binary validation",
                                visible=True,
                                interactive=True,
                            )
                            regexp_pattern_input = gr.Textbox(
                                label="RegExp Pattern",
                                placeholder=r"Example: Answer:\s*(.+?)$  - extracts text after 'Answer:'",
                                visible=False,
                                lines=2,
                                info="Regular expression with capture group to extract and compare with ground truth",
                            )

                        with gr.Group() as continuous_validation_group:
                            continuous_metric_input = gr.Dropdown(
                                choices=[
                                    "ROUGE-1",
                                    "ROUGE-2",
                                    "ROUGE-L",
                                    "BERTScore",
                                    "BLEU",
                                ],
                                label="Continuous Validation Metric",
                                info="Metric for continuous validation",
                                visible=False,
                                interactive=True,
                            )

            gr.Markdown("### 🧠 Memory Configuration")
            with gr.Row():
                enable_memory_checkbox = gr.Checkbox(
                    label="Enable Memory Retrieval",
                    value=False,
                    info="Retrieve relevant ideas from memory bank during evolution mutations",
                )
            with gr.Row():
                memory_namespace_input = gr.Textbox(
                    label="Memory Namespace",
                    placeholder="Optional; defaults to experiment ID",
                    info="Optional. Used only when memory retrieval is enabled; leave empty to use the experiment ID.",
                )

            with gr.Row():
                create_btn = gr.Button("Create Experiment", variant="primary", size="lg")
                clean_btn = gr.Button("🧹 Clean Form", variant="secondary", size="lg")

            create_output = gr.Textbox(
                label="Status",
                interactive=False,
                info="Experiment creation status and results",
                lines=4,
            )

            current_columns_state = gr.State(value=[])
            clicked_columns_state = gr.State(value=[])
            button_offset_state = gr.State(value=0)
            preset_data_path_state = gr.State(value="")
            max_dataset_size_state = gr.State(value=None)

            self._setup_event_handlers(
                data_file_input,
                target_field_input,
                dataset_info,
                name_input,
                description_input,
                max_iterations_input,
                llm_model_input,
                prompt_llm_model_input,
                base_prompt_input,
                column_buttons_row,
                no_columns_msg,
                generate_baseline_btn,
                col_btn_1,
                col_btn_2,
                col_btn_3,
                col_btn_4,
                col_btn_5,
                col_btn_6,
                col_btn_7,
                col_btn_8,
                current_columns_state,
                clicked_columns_state,
                button_offset_state,
                validation_type_input,
                binary_validation_group,
                binary_validation_method_input,
                regexp_pattern_input,
                continuous_validation_group,
                continuous_metric_input,
                create_btn,
                clean_btn,
                create_output,
                preset_data_path_state,
                dataset_info,
                dataset_size_input,
                test_size_input,
                split_info,
                max_dataset_size_state,
                btn_cmns,
                btn_emot,
                btn_gsm8k,
                btn_sent,
                btn_xsum,
                enable_memory_checkbox,
                memory_namespace_input,
            )

        return component

    def _setup_event_handlers(self, *inputs):
        """Set up all event handlers for the component."""
        (
            data_file_input,
            target_field_input,
            dataset_info,
            name_input,
            description_input,
            max_iterations_input,
            llm_model_input,
            prompt_llm_model_input,
            base_prompt_input,
            column_buttons_row,
            no_columns_msg,
            generate_baseline_btn,
            col_btn_1,
            col_btn_2,
            col_btn_3,
            col_btn_4,
            col_btn_5,
            col_btn_6,
            col_btn_7,
            col_btn_8,
            current_columns_state,
            clicked_columns_state,
            button_offset_state,
            validation_type_input,
            binary_validation_group,
            binary_validation_method_input,
            regexp_pattern_input,
            continuous_validation_group,
            continuous_metric_input,
            create_btn,
            clean_btn,
            create_output,
            preset_data_path_state,
            dataset_info,
            dataset_size_input,
            test_size_input,
            split_info,
            max_dataset_size_state,
            btn_cmns,
            btn_emot,
            btn_gsm8k,
            btn_sent,
            btn_xsum,
            enable_memory_checkbox,
            memory_namespace_input,
        ) = inputs

        column_buttons = [col_btn_1, col_btn_2, col_btn_3, col_btn_4, col_btn_5, col_btn_6, col_btn_7, col_btn_8]

        def _update_file_and_columns(file):
            src_path = extract_source_path_from_upload(file)
            file_columns = read_csv_columns(src_path) if src_path else []

            dataset_info_update = gr.update()
            dataset_size_update = gr.update(value=None)
            new_max_size = None

            if src_path:
                filename = os.path.basename(src_path)
                dataset_info_update = gr.update(value=f"📁 Using uploaded file: {filename}")

                try:
                    row_count = count_csv_rows(src_path)
                    if row_count is not None and row_count > 0:
                        logger.info(f"Counted {row_count} rows in uploaded file: {src_path}")
                        dataset_size_update = gr.update(value=row_count)
                        new_max_size = row_count
                except Exception as e:
                    logger.warning(f"Failed to count rows in uploaded file {src_path}: {e}")

            target_choices = get_default_target_choices("classification", file_columns)
            target_update = gr.update(
                choices=target_choices,
                value=target_choices[0] if target_choices else None,
                visible=bool(target_choices),
            )

            clicked_columns = []
            button_offset = 0

            if file_columns:
                target_col = target_choices[0] if target_choices else None
                available_columns = [col for col in file_columns if col != target_col]
                visible_columns = available_columns[:8]

                button_updates = []
                for col in visible_columns:
                    button_updates.append(gr.update(value=col, visible=True, interactive=True))
                for _ in range(len(visible_columns), 8):
                    button_updates.append(gr.update(visible=False))

                return (
                    dataset_info_update,
                    target_update,
                    gr.update(visible=True),
                    gr.update(visible=False),
                    gr.update(visible=True, interactive=True),
                    *button_updates,
                    file_columns,
                    clicked_columns,
                    button_offset,
                    dataset_size_update,
                    new_max_size,
                )
            else:
                button_updates = [gr.update(visible=False) for _ in range(8)]
                return (
                    dataset_info_update,
                    target_update,
                    gr.update(visible=False),
                    gr.update(visible=True),
                    gr.update(visible=False),
                    *button_updates,
                    [],
                    [],
                    0,
                    dataset_size_update,
                    None,
                )

        data_file_input.change(
            _update_file_and_columns,
            inputs=[data_file_input],
            outputs=[
                dataset_info,
                target_field_input,
                column_buttons_row,
                no_columns_msg,
                generate_baseline_btn,
                *column_buttons,
                current_columns_state,
                clicked_columns_state,
                button_offset_state,
                dataset_size_input,
                max_dataset_size_state,
            ],
        )

        def _update_split_info(dataset_size, test_size, data_file, max_size):
            if (
                dataset_size is None
                or dataset_size == ""
                or (isinstance(dataset_size, (int, float)) and dataset_size <= 0)
            ):
                return gr.update(value="", visible=False)

            try:
                dataset_size = int(float(dataset_size))
                if dataset_size <= 0:
                    return gr.update(value="", visible=False)

                min_size = None
                if max_size is not None and max_size > 0:
                    min_size = max(1, int(max_size * 0.1))

                    if dataset_size > max_size:
                        dataset_size = max_size
                        logger.warning(f"Dataset size exceeds maximum {max_size}, using {max_size}")

                    if dataset_size < min_size:
                        dataset_size = min_size
                        logger.warning(
                            f"Dataset size is less than minimum {min_size} (10% of {max_size}), using {min_size}"
                        )

                test_size_ratio = float(test_size) if test_size is not None else 0.2
                train_size_ratio = 1.0 - test_size_ratio

                train_rows = int(dataset_size * train_size_ratio)
                test_rows = int(dataset_size * test_size_ratio)

                info_text = f"**Split Preview:**\n- Train: {train_rows} rows ({train_size_ratio * 100:.1f}%)\n- Test: {test_rows} rows ({test_size_ratio * 100:.1f}%)\n- Total: {dataset_size} rows"
                if max_size is not None:
                    info_text += f"\n- Max available: {max_size} rows"
                    if min_size is not None:
                        info_text += f"\n- Minimum required: {min_size} rows (10% of dataset)"
                return gr.update(value=info_text, visible=True)
            except (ValueError, TypeError):
                return gr.update(value="", visible=False)

        def _update_split_info_with_validation(dataset_size, test_size, data_file, max_size):
            """Update split info and validate dataset size (min 10%, max 100%)."""
            result = _update_split_info(dataset_size, test_size, data_file, max_size)

            try:
                dataset_size_int = int(float(dataset_size)) if dataset_size not in (None, "") else None
                if dataset_size_int and max_size and max_size > 0:
                    min_size = max(1, int(max_size * 0.1))
                    if dataset_size_int > max_size:
                        return gr.update(value=max_size), result
                    elif dataset_size_int < min_size:
                        return gr.update(value=min_size), result
            except (ValueError, TypeError):
                pass

            return gr.update(), result

        dataset_size_input.change(
            _update_split_info_with_validation,
            inputs=[dataset_size_input, test_size_input, data_file_input, max_dataset_size_state],
            outputs=[dataset_size_input, split_info],
        )
        test_size_input.change(
            _update_split_info,
            inputs=[dataset_size_input, test_size_input, data_file_input, max_dataset_size_state],
            outputs=[split_info],
        )

        def _update_column_buttons_exclude_target(target_column, all_columns):
            if not all_columns:
                button_updates = [gr.update(visible=False) for _ in range(8)]
                return (
                    [gr.update(visible=False), gr.update(visible=True), gr.update(visible=False)]
                    + button_updates
                    + [[], 0]
                )

            available_columns = [col for col in all_columns if col != target_column]
            visible_columns = available_columns[:8]

            button_updates = []
            for col in visible_columns:
                button_updates.append(gr.update(value=col, visible=True, interactive=True))
            for _ in range(len(visible_columns), 8):
                button_updates.append(gr.update(visible=False))

            return (
                [gr.update(visible=True), gr.update(visible=False), gr.update(visible=True)] + button_updates + [[], 0]
            )

        target_field_input.change(
            _update_column_buttons_exclude_target,
            inputs=[target_field_input, current_columns_state],
            outputs=[
                column_buttons_row,
                no_columns_msg,
                generate_baseline_btn,
                *column_buttons,
                clicked_columns_state,
                button_offset_state,
            ],
        )

        def _handle_validation_type_change(validation_type):
            if validation_type == "Binary (0/1)":
                return (
                    gr.update(visible=True),
                    gr.update(value="equality", visible=True),
                    gr.update(visible=False),
                    gr.update(visible=False),
                    gr.update(value=None, visible=False),
                )
            elif validation_type == "Continuous (0..1)":
                return (
                    gr.update(visible=False),
                    gr.update(value=None, visible=False),
                    gr.update(visible=False),
                    gr.update(visible=True),
                    gr.update(value="ROUGE-1", visible=True),
                )
            else:
                return (
                    gr.update(visible=False),
                    gr.update(value=None, visible=False),
                    gr.update(visible=False),
                    gr.update(visible=False),
                    gr.update(value=None, visible=False),
                )

        validation_type_input.change(
            _handle_validation_type_change,
            inputs=[validation_type_input],
            outputs=[
                binary_validation_group,
                binary_validation_method_input,
                regexp_pattern_input,
                continuous_validation_group,
                continuous_metric_input,
            ],
        )

        def _handle_binary_method_change(binary_method):
            if binary_method == "regexp":
                return gr.update(visible=True)
            else:
                return gr.update(visible=False)

        binary_validation_method_input.change(
            _handle_binary_method_change,
            inputs=[binary_validation_method_input],
            outputs=[regexp_pattern_input],
        )

        for i, btn in enumerate(column_buttons):

            def handle_column_click(
                column_name, current_prompt, all_columns, clicked_columns, button_offset, target_column
            ):
                if not column_name:
                    return current_prompt, clicked_columns, button_offset, *[gr.update() for _ in column_buttons]

                new_clicked_columns = clicked_columns + [column_name]

                updated_prompt = self._insert_column_template(current_prompt, column_name)

                available_columns = [
                    col for col in all_columns if col != target_column and col not in new_clicked_columns
                ]
                next_columns = available_columns[button_offset : button_offset + 8]

                button_updates = []
                for i, btn in enumerate(column_buttons):
                    if i < len(next_columns):
                        button_updates.append(gr.update(value=next_columns[i], visible=True, interactive=True))
                    else:
                        button_updates.append(gr.update(visible=False))

                new_button_offset = button_offset + 8 if len(available_columns) > button_offset + 8 else button_offset

                return [updated_prompt, new_clicked_columns, new_button_offset] + button_updates

            btn.click(
                handle_column_click,
                inputs=[
                    btn,
                    base_prompt_input,
                    current_columns_state,
                    clicked_columns_state,
                    button_offset_state,
                    target_field_input,
                ],
                outputs=[base_prompt_input, clicked_columns_state, button_offset_state, *column_buttons],
            )

        def _generate_baseline_prompt(all_columns, target_column):
            """Generate a baseline prompt using available columns."""
            if not all_columns or not target_column:
                return gr.update(value="")

            feature_columns = [col for col in all_columns if col != target_column]

            if not feature_columns:
                return gr.update(value="")

            key_features = feature_columns
            features_text = "\n".join([f"- {{{col}}}" for col in key_features])

            baseline_prompt = f"""You are a machine learning assistant. Analyze the provided features to predict the target value.

Available Features:
{features_text}

Target Variable: {{{target_column}}}

Instructions:
1. Analyze the relationship between the features and target
2. Consider all available information
3. Provide your prediction for '{target_column}'

What is your prediction?

Answer: [expected answer]"""

            return gr.update(value=baseline_prompt)

        generate_baseline_btn.click(
            _generate_baseline_prompt,
            inputs=[current_columns_state, target_field_input],
            outputs=[base_prompt_input],
        )

        def _clean_form():
            return (
                gr.update(value=""),
                gr.update(value=""),
                gr.update(value=None),
                gr.update(choices=[], value=None, visible=False),
                gr.update(value="No dataset selected"),
                gr.update(value=100),
                gr.update(value=get_default_llm_model_id()),
                gr.update(value=get_default_prompt_llm_model_id()),
                gr.update(value=""),
                gr.update(visible=False),
                gr.update(visible=True),
                gr.update(visible=False),
                *[gr.update(visible=False) for _ in range(8)],
                [],
                [],
                0,
                gr.update(value=None),
                gr.update(visible=False),
                gr.update(value=None),
                gr.update(value=""),
                gr.update(visible=False),
                gr.update(value=None),
                gr.update(value=None),
                gr.update(value=0.2),
                gr.update(value="", visible=False),
                gr.update(value=""),
            )

        clean_btn.click(
            _clean_form,
            outputs=[
                name_input,
                description_input,
                data_file_input,
                target_field_input,
                dataset_info,
                max_iterations_input,
                llm_model_input,
                prompt_llm_model_input,
                base_prompt_input,
                column_buttons_row,
                no_columns_msg,
                generate_baseline_btn,
                *column_buttons,
                current_columns_state,
                clicked_columns_state,
                button_offset_state,
                validation_type_input,
                binary_validation_group,
                binary_validation_method_input,
                regexp_pattern_input,
                continuous_validation_group,
                continuous_metric_input,
                dataset_size_input,
                test_size_input,
                split_info,
                create_output,
            ],
        )

        create_btn.click(
            self._create_prompt_experiment,
            inputs=[
                name_input,
                description_input,
                data_file_input,
                max_iterations_input,
                llm_model_input,
                prompt_llm_model_input,
                target_field_input,
                base_prompt_input,
                current_columns_state,
                validation_type_input,
                binary_validation_method_input,
                regexp_pattern_input,
                continuous_metric_input,
                preset_data_path_state,
                dataset_size_input,
                test_size_input,
                enable_memory_checkbox,
                memory_namespace_input,
            ],
            outputs=create_output,
        )

        def _init_local_presets():
            """Fetch and populate local preset buttons with names."""
            examples = self.exp_manager.list_local_prompt_presets()
            names = [e.get("name") for e in examples][:8]
            updates = []
            for i in range(8):
                if i < len(names):
                    label = names[i].replace("_", " ").title()
                    updates.append(gr.update(value=label, visible=True))
                else:
                    updates.append(gr.update(visible=False))
            return updates

        try:
            _init_local_presets()
        except Exception:
            pass

        def _prefill_from_local_preset(label, current_name, current_desc):
            """Fetch preset by label -> name; prefill name and base prompt."""
            if not label:
                return gr.update(), gr.update(), gr.update(visible=False, value="")
            name = label.lower().replace(" ", "_")
            details = self.exp_manager.get_local_prompt_preset(name)
            if not details:
                return gr.update(), gr.update(), gr.update(visible=True, value=f"Error: failed to load '{label}'")
            baseline_prompt = details.get("baseline_prompt") or ""
            dataset = details.get("dataset") or {}
            ds_text = f"Preset dataset: {dataset.get('train')}" if dataset.get("train") else "Preset dataset: (none)"
            new_name = current_name or details.get("label") or label
            return (
                gr.update(value=new_name),
                gr.update(value=baseline_prompt),
                gr.update(visible=True, value=f"{ds_text}"),
            )

        def _prefill_fixed_task(task_name):
            details = self.exp_manager.get_local_prompt_preset(task_name)
            if not details:
                return (
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    "",
                    gr.update(visible=True, value=f"Error: failed to load example '{task_name}'"),
                    gr.update(value=None),
                    gr.update(value=0.2),
                    gr.update(value="", visible=False),
                    None,
                )

            up = self.exp_manager.upload_local_prompt_preset_dataset(task_name)
            data_path = up.get("data_path", "") if isinstance(up, dict) else ""
            public_url = ""
            try:
                if data_path:
                    base = S3_API_URL.rstrip("/")
                    bucket = STORAGE_BUCKET_NAME
                    public_url = f"{base}/{bucket}/{data_path}"
            except Exception:
                public_url = ""
            if public_url:
                ds_msg = f"📁 Preset dataset URL:\n{public_url}"
            else:
                ds_msg = (
                    f"📁 Using preset dataset: {up.get('filename')}"
                    if isinstance(up, dict) and "filename" in up
                    else "📁 Preset dataset uploaded"
                )

            label = details.get("label") or task_name.replace("_", " ").title()
            desc = details.get("task_description") or ""
            base_prompt = details.get("baseline_prompt") or ""
            target_field = details.get("target_field") or ""

            dataset_size_update = gr.update(value=None)
            max_size = None
            if data_path:
                try:
                    base = INTERNAL_S3_API_URL.rstrip("/")
                    bucket = STORAGE_BUCKET_NAME
                    url = f"{base}/{bucket}/{data_path}"
                    logger.debug(f"Downloading preset dataset for row count from: {url}")
                    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                        tmp_path = tmp.name
                    try:
                        resp = requests.get(url, timeout=30)
                        resp.raise_for_status()
                        with open(tmp_path, "wb") as f:
                            f.write(resp.content)
                        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                            train_rows = count_csv_rows(tmp_path)
                            if train_rows is not None and train_rows > 0:
                                val_path = data_path.replace("/train.csv", "/val.csv")
                                total_rows = train_rows

                                try:
                                    val_url = f"{base}/{bucket}/{val_path}"
                                    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as val_tmp:
                                        val_tmp_path = val_tmp.name
                                    try:
                                        val_resp = requests.get(val_url, timeout=30)
                                        if val_resp.ok:
                                            with open(val_tmp_path, "wb") as f:
                                                f.write(val_resp.content)
                                            val_rows = count_csv_rows(val_tmp_path)
                                            if val_rows is not None and val_rows > 0:
                                                total_rows = train_rows + val_rows
                                    finally:
                                        try:
                                            if os.path.exists(val_tmp_path):
                                                os.unlink(val_tmp_path)
                                        except Exception:
                                            pass
                                except Exception as val_err:
                                    logger.debug(
                                        f"val.csv not available for {task_name}, using train.csv rows only: {val_err}"
                                    )
                                    total_rows = train_rows

                                logger.info(
                                    f"Counted {total_rows} total rows in prompt preset dataset (train: {train_rows}, total: {total_rows})"
                                )
                                dataset_size_update = gr.update(value=total_rows)
                                max_size = total_rows
                            else:
                                logger.warning(f"Failed to count rows or empty dataset: {data_path}")
                        else:
                            logger.error(f"Downloaded file is empty or missing: {tmp_path}")
                    finally:
                        try:
                            if os.path.exists(tmp_path):
                                os.unlink(tmp_path)
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"Failed to count rows for preset dataset {data_path}: {e}", exc_info=True)

            return (
                gr.update(value=label),
                gr.update(value=desc),
                gr.update(value=base_prompt),
                gr.update(value=target_field, choices=[target_field]),
                data_path,
                gr.update(value=ds_msg),
                dataset_size_update,
                gr.update(value=0.2),
                gr.update(value="", visible=False),
                max_size,
            )

        for btn, task in [
            (btn_cmns, "commonsense"),
            (btn_emot, "emotion"),
            (btn_gsm8k, "gsm8k"),
            (btn_sent, "sentiment_analysis"),
            (btn_xsum, "xsum"),
        ]:

            def _make_prefill(task_name: str):
                return lambda: _prefill_fixed_task(task_name)

            btn.click(
                _make_prefill(task),
                inputs=[],
                outputs=[
                    name_input,
                    description_input,
                    base_prompt_input,
                    target_field_input,
                    preset_data_path_state,
                    dataset_info,
                    dataset_size_input,
                    test_size_input,
                    split_info,
                    max_dataset_size_state,
                ],
            )

    def _insert_column_template(self, current_prompt: str, column_name: str) -> str:
        """Insert column template into prompt.

        Args:
            current_prompt: Current prompt text
            column_name: Column name to insert as template

        Returns:
            Updated prompt with template inserted
        """
        if not column_name:
            return current_prompt

        template = f"{{{column_name}}}"
        if current_prompt:
            if not current_prompt.endswith(" ") and not current_prompt.endswith("\n"):
                return f"{current_prompt} {template}"
            else:
                return f"{current_prompt}{template}"
        else:
            return template

    def _validate_prompt_template(self, prompt: str, available_columns: list, target_column: str) -> tuple[bool, str]:
        """Validate that prompt template is properly formatted.

        Args:
            prompt: Base prompt text
            available_columns: List of available column names
            target_column: Target column name

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not prompt or not prompt.strip():
            return False, "Base prompt cannot be empty"

        template_pattern = r"\{([^}]+)\}"
        placeholders = re.findall(template_pattern, prompt)

        if not placeholders:
            return False, "Prompt must contain at least one template placeholder in {column_name} format"

        invalid_placeholders = []
        for placeholder in placeholders:
            if placeholder not in available_columns:
                invalid_placeholders.append(placeholder)

        if invalid_placeholders:
            return (
                False,
                f"Invalid template placeholders: {', '.join(invalid_placeholders)}. Available columns: {', '.join(available_columns)}",
            )

        if target_column and target_column not in placeholders:
            logger.warning(f"Target column '{target_column}' is not included in prompt template")

        return True, ""

    def _create_prompt_experiment(
        self,
        name: str,
        description: str,
        data_file,
        max_iterations: int,
        llm_model: str,
        prompt_llm_model: str,
        target_field: str,
        base_prompt: str,
        available_columns: list,
        validation_type: str,
        binary_validation_method: str,
        regexp_pattern: str,
        continuous_metric: str,
        preset_data_path: str,
        dataset_size: Optional[float],
        test_size: float,
        enable_memory: bool = False,
        memory_namespace: str = "",
    ) -> str:
        """Create a new prompt-based experiment using the /prompts endpoint.

        Args:
            name: Experiment name
            description: Experiment description
            data_file: Uploaded data file
            max_iterations: Maximum iterations
            llm_model: LLM model to use
            target_field: Target column
            base_prompt: Base prompt with templates
            available_columns: List of available columns
            validation_type: Validation type (Binary/Continuous)
            binary_validation_method: Binary validation method
            regexp_pattern: RegExp pattern for binary validation
            continuous_metric: Continuous validation metric

        Returns:
            Status message
        """
        is_valid, error = validate_experiment_name(name)
        if not is_valid:
            return f"Error: {error}"

        is_valid, error = validate_max_iterations(max_iterations)
        if not is_valid:
            return f"Error: {error}"

        try:
            if (not available_columns) and preset_data_path:
                base = INTERNAL_S3_API_URL.rstrip("/")
                bucket = STORAGE_BUCKET_NAME
                url = f"{base}/{bucket}/{preset_data_path}"
                with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    resp = requests.get(url, timeout=10)
                    resp.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        f.write(resp.content)
                    available_columns = read_csv_columns(tmp_path) or []
                finally:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
        except Exception:
            pass

        is_valid, error = self._validate_prompt_template(base_prompt, available_columns, target_field)
        if not is_valid:
            return f"Error: {error}"

        if not data_file and not preset_data_path:
            return "Error: Please upload a data file or choose a preset"

        src_path = extract_source_path_from_upload(data_file) if data_file else None
        if src_path:
            is_valid, error = validate_file_upload(src_path, [".csv", ".json", ".txt"])
            if not is_valid:
                return f"Error: {error}"

        if not target_field:
            return "Error: Please select a target column"

        if binary_validation_method == "regexp":
            is_valid, error = validate_regexp_pattern(regexp_pattern)
            if not is_valid:
                return f"Error: {error}"

        try:
            data_path = preset_data_path or ""
            max_dataset_size = None

            if not data_path and data_file is not None:
                src_path = extract_source_path_from_upload(data_file)

                if src_path and os.path.exists(src_path):
                    filename = os.path.basename(src_path)

                    try:
                        max_dataset_size = count_csv_rows(src_path)
                    except Exception as e:
                        logger.warning(f"Failed to count rows in uploaded file {src_path}: {e}")

                    upload_result = self.exp_manager.upload_data_file(src_path, filename)

                    if "error" in upload_result:
                        return f"Error uploading file to storage: {upload_result['error']}"

                    data_path = upload_result.get("data_path", "")
                    cleanup_temp_file(src_path)
                else:
                    return "Error: Uploaded file is not accessible"
            elif preset_data_path:
                try:
                    base = INTERNAL_S3_API_URL.rstrip("/")
                    bucket = STORAGE_BUCKET_NAME
                    url = f"{base}/{bucket}/{preset_data_path}"
                    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                        tmp_path = tmp.name
                    try:
                        resp = requests.get(url, timeout=30)
                        resp.raise_for_status()
                        with open(tmp_path, "wb") as f:
                            f.write(resp.content)
                        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                            train_rows = count_csv_rows(tmp_path)
                            if train_rows is not None and train_rows > 0:
                                val_path = preset_data_path.replace("/train.csv", "/val.csv")
                                total_rows = train_rows
                                try:
                                    val_url = f"{base}/{bucket}/{val_path}"
                                    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as val_tmp:
                                        val_tmp_path = val_tmp.name
                                    try:
                                        val_resp = requests.get(val_url, timeout=30)
                                        if val_resp.ok:
                                            with open(val_tmp_path, "wb") as f:
                                                f.write(val_resp.content)
                                            val_rows = count_csv_rows(val_tmp_path)
                                            if val_rows is not None and val_rows > 0:
                                                total_rows = train_rows + val_rows
                                    finally:
                                        try:
                                            if os.path.exists(val_tmp_path):
                                                os.unlink(val_tmp_path)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                max_dataset_size = total_rows
                    finally:
                        try:
                            if os.path.exists(tmp_path):
                                os.unlink(tmp_path)
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"Failed to count rows for preset dataset {preset_data_path}: {e}")

            if dataset_size is not None and dataset_size > 0:
                dataset_size_int = int(dataset_size)
                if max_dataset_size is not None and max_dataset_size > 0:
                    min_size = max(1, int(max_dataset_size * 0.1))
                    if dataset_size_int > max_dataset_size:
                        return f"Error: Dataset size ({dataset_size_int}) exceeds maximum available ({max_dataset_size} rows). Please use a value between {min_size} and {max_dataset_size}."
                    if dataset_size_int < min_size:
                        return f"Error: Dataset size ({dataset_size_int}) is less than minimum required ({min_size} rows, 10% of dataset). Please use a value between {min_size} and {max_dataset_size}."

            validation_criteria = {
                "validation_type": validation_type,
                "binary_method": binary_validation_method,
                "regexp_pattern": regexp_pattern,
                "continuous_metric": continuous_metric,
            }

            prompt_experiment_data = {
                "name": name,
                "description": description,
                "data_path": data_path,
                "target_column": target_field,
                "base_prompt": base_prompt,
                "validation_criteria": validation_criteria,
                "llm_model": llm_model,
                "prompt_llm_model": prompt_llm_model,
                "max_iterations": max_iterations,
                "enable_memory": bool(enable_memory),
            }
            if str(memory_namespace or "").strip():
                prompt_experiment_data["memory_namespace"] = str(memory_namespace).strip()

            if dataset_size is not None and dataset_size > 0:
                prompt_experiment_data["dataset_size"] = int(dataset_size)
            if test_size is not None:
                prompt_experiment_data["test_size"] = float(test_size)

            result = self.exp_manager.create_prompt_experiment(prompt_experiment_data)

            if "error" in result:
                return f"Error: {result['error']}"

            placeholder_count = len(re.findall(r"\{([^}]+)\}", base_prompt))
            return f"✅ Prompt experiment '{name}' created successfully with ID: {result['id']}\nFile uploaded to storage: {data_path}\nTarget: {target_field}\nValidation type: {validation_type}\nTemplate placeholders: {placeholder_count}"

        except Exception as e:
            logger.error(f"Error creating prompt experiment: {e}")
            return f"Error creating prompt experiment: {str(e)}"

    def test_master_api_connection(self) -> str:
        """Test Master API connection and return status message.

        Returns:
            Status message for display
        """
        try:
            result = self.exp_manager.test_api_connection()
            status = result.get("status", "unknown")
            message = result.get("message", "Unknown status")

            if status == "success":
                return f"{message}\n📊 Ready to create prompt experiments"
            else:
                suggestion = result.get("suggestion", "")
                return f"{message}\n💡 {suggestion}" if suggestion else message

        except Exception as e:
            logger.error(f"Error testing Master API connection: {e}")
            return f"❌ Failed to test API connection: {str(e)}\n💡 Try running 'make dev' to start all services"
