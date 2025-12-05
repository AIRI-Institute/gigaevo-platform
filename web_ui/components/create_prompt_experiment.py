"""Create Prompt Experiment tab component for LLM prompt evolution."""

import os
import re
import tempfile
import requests

import gradio as gr
from config.settings import (
    AVAILABLE_LLM_MODELS,
    VALIDATION_RULES,
    S3_API_URL,
    STORAGE_BUCKET_NAME,
    INTERNAL_S3_API_URL,
)
from loguru import logger
from utils.file_handlers import (
    cleanup_temp_file,
    extract_source_path_from_upload,
    read_csv_columns,
)
from utils.validators import (
    get_default_target_choices,
    validate_experiment_name,
    validate_file_upload,
    validate_max_iterations,
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
                        choices=AVAILABLE_LLM_MODELS,
                        value=AVAILABLE_LLM_MODELS[0],
                        label="LLM Model",
                        info="LLM model to use for prompt evolution",
                    )

            with gr.Row():
                with gr.Column(scale=2):
                    # Base prompt section
                    gr.Markdown("### Prompt Template")

                    # (Local Prompt Presets dropdown removed; only top preset buttons are used)

                    # Example section
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

                # Available columns section
                with gr.Column(scale=1):
                    gr.Markdown("#### Available Columns (click to add to prompt)")
                    column_buttons_container = gr.Group()
                    with column_buttons_container:
                        # This will be populated dynamically based on uploaded file
                        column_buttons_row = gr.Row(visible=False, elem_classes=["column-buttons-row"])
                        with column_buttons_row:
                            # We'll add up to 8 column buttons dynamically
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

                    # TEMPORARY: Simplified Task Type section
                    # FUTURE: Uncomment the complex Validation Criteria section below when needed
                    gr.Markdown("#### 🎯 Task Type")
                    task_type_input = gr.Dropdown(
                        choices=["classification", "multi_choice", "math", "summarization"],
                        label="Task Type",
                        info="Select the type of task for this experiment",
                        interactive=True,
                        value="classification",
                    )

                    # FUTURE: Uncomment this complex Validation Criteria section when needed
                    """
                    # Validation Criteria section
                    gr.Markdown("#### 🔧 Validation Criteria")
                    with gr.Group():
                        validation_type_input = gr.Dropdown(
                            choices=["Binary (0/1)", "Continuous (0..1)"],
                            label="Validation Type",
                            info="Select how the target column should be validated",
                            interactive=True,
                        )

                        # Binary validation options
                        with gr.Group() as binary_validation_group:
                            binary_validation_method_input = gr.Dropdown(
                                choices=["equality", "occurrence of a substring", "RegExp"],
                                label="Binary Validation Method",
                                info="Method for binary validation",
                                visible=True,
                                interactive=True,
                            )
                            regexp_pattern_input = gr.Textbox(
                                label="RegExp Pattern",
                                placeholder="Enter regular expression pattern to extract target substring...",
                                visible=False,
                                lines=2,
                                info="Regular expression to match the target substring",
                            )

                        # Continuous validation options
                        with gr.Group() as continuous_validation_group:
                            continuous_metric_input = gr.Dropdown(
                                choices=[
                                    "ROUGE-1",
                                    "ROUGE-2",
                                    "ROUGE-L",
                                    "ROUGE-Lsum",
                                    "METEOR",
                                    "BERTScore",
                                    "AlignScore",
                                ],
                                label="Continuous Validation Metric",
                                info="Metric for continuous validation",
                                visible=False,
                                interactive=True,
                            )
                    """

            # Action buttons
            with gr.Row():
                create_btn = gr.Button("Create Experiment", variant="primary", size="lg")
                clean_btn = gr.Button("🧹 Clean Form", variant="secondary", size="lg")

            create_output = gr.Textbox(
                label="Status",
                interactive=False,
                info="Experiment creation status and results",
                lines=4,
            )

            # Store current columns for template insertion
            current_columns_state = gr.State(value=[])
            # Track clicked columns and button offset for rotation
            clicked_columns_state = gr.State(value=[])
            button_offset_state = gr.State(value=0)
            # Store preset data_path (when dataset is uploaded by backend)
            preset_data_path_state = gr.State(value="")

            # Wire up event handlers
            self._setup_event_handlers(
                data_file_input,
                target_field_input,
                dataset_info,
                name_input,
                description_input,
                max_iterations_input,
                llm_model_input,
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
                task_type_input,
                # FUTURE: Uncomment these validation criteria parameters when needed
                # validation_type_input,
                # binary_validation_group,
                # binary_validation_method_input,
                # regexp_pattern_input,
                # continuous_validation_group,
                # continuous_metric_input,
                create_btn,
                clean_btn,
                create_output,
                preset_data_path_state,
                dataset_info,
                btn_cmns,
                btn_emot,
                btn_gsm8k,
                btn_sent,
                btn_xsum,
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
            task_type_input,
            # FUTURE: Uncomment these validation criteria parameters when needed
            # validation_type_input,
            # binary_validation_group,
            # binary_validation_method_input,
            # regexp_pattern_input,
            # continuous_validation_group,
            # continuous_metric_input,
            create_btn,
            clean_btn,
            create_output,
            preset_data_path_state,
            dataset_info,
            btn_cmns,
            btn_emot,
            btn_gsm8k,
            btn_sent,
            btn_xsum,
        ) = inputs

        column_buttons = [col_btn_1, col_btn_2, col_btn_3, col_btn_4, col_btn_5, col_btn_6, col_btn_7, col_btn_8]

        # Update target choices when file changes
        def _update_file_and_columns(file):
            src_path = extract_source_path_from_upload(file)
            file_columns = read_csv_columns(src_path) if src_path else []

            # Update dataset info
            dataset_info_update = gr.update()
            if src_path:
                filename = os.path.basename(src_path)
                dataset_info_update = gr.update(value=f"📁 Using uploaded file: {filename}")

            # Update target dropdown
            target_choices = get_default_target_choices("classification", file_columns)
            target_update = gr.update(
                choices=target_choices,
                value=target_choices[0] if target_choices else None,
                visible=bool(target_choices),
            )

            # Initialize state for column tracking
            clicked_columns = []
            button_offset = 0

            # Update column buttons and generate baseline button
            if file_columns:
                # Show first 8 columns as buttons (excluding target initially)
                target_col = target_choices[0] if target_choices else None
                available_columns = [col for col in file_columns if col != target_col]
                visible_columns = available_columns[:8]

                button_updates = []
                for col in visible_columns:
                    button_updates.append(gr.update(value=col, visible=True, interactive=True))
                # Hide remaining buttons
                for _ in range(len(visible_columns), 8):
                    button_updates.append(gr.update(visible=False))

                return (
                    dataset_info_update,
                    target_update,
                    gr.update(visible=True),  # column_buttons_row
                    gr.update(visible=False),  # no_columns_msg
                    gr.update(visible=True, interactive=True),  # generate_baseline_btn
                    *button_updates,
                    file_columns,  # current_columns_state
                    clicked_columns,  # clicked_columns_state
                    button_offset,  # button_offset_state
                )
            else:
                # Hide all column buttons and generate baseline button
                button_updates = [gr.update(visible=False) for _ in range(8)]
                return (
                    dataset_info_update,
                    target_update,
                    gr.update(visible=False),  # column_buttons_row
                    gr.update(visible=True),  # no_columns_msg
                    gr.update(visible=False),  # generate_baseline_btn
                    *button_updates,
                    [],  # current_columns_state
                    [],  # clicked_columns_state
                    0,  # button_offset_state
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
            ],
        )

        # Update column buttons when target changes (exclude target column)
        def _update_column_buttons_exclude_target(target_column, all_columns):
            if not all_columns:
                # Hide all buttons and baseline button
                button_updates = [gr.update(visible=False) for _ in range(8)]
                return (
                    [gr.update(visible=False), gr.update(visible=True), gr.update(visible=False)]
                    + button_updates
                    + [[], 0]
                )

            # Filter out target column
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

        # FUTURE: Uncomment these validation criteria event handlers when needed
        """
        # Handle validation type change for cascading behavior
        def _handle_validation_type_change(validation_type):
            # Handle validation type selection change.
            if validation_type == "Binary (0/1)":
                return (
                    gr.update(visible=True),  # binary_validation_group
                    gr.update(value="equality", visible=True),  # binary_validation_method_input
                    gr.update(visible=False),  # regexp_pattern_input
                    gr.update(visible=False),  # continuous_validation_group
                    gr.update(visible=True),  # continuous_metric_input
                )
            elif validation_type == "Continuous (0..1)":
                return (
                    gr.update(visible=False),  # binary_validation_group
                    gr.update(visible=True),  # binary_validation_method_input
                    gr.update(visible=False),  # regexp_pattern_input
                    gr.update(visible=True),  # continuous_validation_group
                    gr.update(value="ROUGE-1", visible=True),  # continuous_metric_input
                )
            else:
                # No selection made
                return (
                    gr.update(visible=False),  # binary_validation_group
                    gr.update(visible=True),  # binary_validation_method_input
                    gr.update(visible=False),  # regexp_pattern_input
                    gr.update(visible=False),  # continuous_validation_group
                    gr.update(visible=True),  # continuous_metric_input
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

        # Handle binary validation method change (show/hide RegExp input)
        def _handle_binary_method_change(binary_method):
            # Handle binary validation method selection change.
            if binary_method == "RegExp":
                return gr.update(visible=True, interactive=True)
            else:
                return gr.update(visible=False)

        binary_validation_method_input.change(
            _handle_binary_method_change,
            inputs=[binary_validation_method_input],
            outputs=[regexp_pattern_input],
        )
        """

        # Set up click handlers for column buttons with proper closure and rotation
        for i, btn in enumerate(column_buttons):

            def handle_column_click(
                column_name, current_prompt, all_columns, clicked_columns, button_offset, target_column
            ):
                if not column_name:  # Button has no value, don't do anything
                    return current_prompt, clicked_columns, button_offset, *[gr.update() for _ in column_buttons]

                # Add column to clicked list
                new_clicked_columns = clicked_columns + [column_name]

                # Insert template into prompt
                updated_prompt = self._insert_column_template(current_prompt, column_name)

                # Calculate next set of columns to show
                available_columns = [
                    col for col in all_columns if col != target_column and col not in new_clicked_columns
                ]
                next_columns = available_columns[button_offset : button_offset + 8]

                # Update buttons with next columns
                button_updates = []
                for i, btn in enumerate(column_buttons):
                    if i < len(next_columns):
                        button_updates.append(gr.update(value=next_columns[i], visible=True, interactive=True))
                    else:
                        button_updates.append(gr.update(visible=False))

                # Update offset for next rotation
                new_button_offset = button_offset + 8 if len(available_columns) > button_offset + 8 else button_offset

                return [updated_prompt, new_clicked_columns, new_button_offset] + button_updates

            btn.click(
                handle_column_click,
                inputs=[
                    btn,  # This passes the button's value directly
                    base_prompt_input,
                    current_columns_state,
                    clicked_columns_state,
                    button_offset_state,
                    target_field_input,
                ],
                outputs=[base_prompt_input, clicked_columns_state, button_offset_state, *column_buttons],
            )

        # Generate baseline prompt handler
        def _generate_baseline_prompt(all_columns, target_column, task_type):
            """Generate a baseline prompt using available columns."""
            if not all_columns or not target_column:
                return gr.update(value="")

            # Filter out target column to get feature columns
            feature_columns = [col for col in all_columns if col != target_column]

            if not feature_columns:
                return gr.update(value="")

            # Create a baseline prompt template
            key_features = feature_columns
            features_text = "\n".join([f"- {{{col}}}" for col in key_features])

            # Add suffix based on task type
            if task_type == "summarization":
                suffix = "Summary:"
            else:
                suffix = "Answer: [expected answer]"

            baseline_prompt = f"""You are a machine learning assistant. Analyze the provided features to predict the target value.

Available Features:
{features_text}

Target Variable: {{{target_column}}}

Instructions:
1. Analyze the relationship between the features and target
2. Consider all available information
3. Provide your prediction for '{target_column}'

What is your prediction?

{suffix}"""

            return gr.update(value=baseline_prompt)

        generate_baseline_btn.click(
            _generate_baseline_prompt,
            inputs=[current_columns_state, target_field_input, task_type_input],
            outputs=[base_prompt_input],
        )

        # Clean form
        def _clean_form():
            return (
                gr.update(value=""),  # name_input
                gr.update(value=""),  # description_input
                gr.update(value=None),  # data_file_input
                gr.update(choices=[], value=None, visible=False),  # target_field_input
                gr.update(value="No dataset selected"),  # dataset_info
                gr.update(value=100),  # max_iterations_input
                gr.update(value=AVAILABLE_LLM_MODELS[0]),  # llm_model_input
                gr.update(value=""),  # base_prompt_input
                gr.update(visible=False),  # column_buttons_row
                gr.update(visible=True),  # no_columns_msg
                gr.update(visible=False),  # generate_baseline_btn
                *[gr.update(visible=False) for _ in range(8)],  # column buttons
                [],  # current_columns_state
                [],  # clicked_columns_state
                0,  # button_offset_state
                gr.update(value="classification"),  # task_type_input - TEMPORARY
                # FUTURE: Uncomment these validation criteria outputs when needed
                # gr.update(value=None),  # validation_type_input
                # gr.update(visible=False),  # binary_validation_group
                # gr.update(value=None),  # binary_validation_method_input
                # gr.update(value=""),  # regexp_pattern_input
                # gr.update(visible=False),  # continuous_validation_group
                # gr.update(value=None),  # continuous_metric_input
                gr.update(value=""),  # create_output
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
                base_prompt_input,
                column_buttons_row,
                no_columns_msg,
                generate_baseline_btn,
                *column_buttons,
                current_columns_state,
                clicked_columns_state,
                button_offset_state,
                task_type_input,
                # FUTURE: Uncomment these validation criteria outputs when needed
                # validation_type_input,
                # binary_validation_group,
                # binary_validation_method_input,
                # regexp_pattern_input,
                # continuous_validation_group,
                # continuous_metric_input,
                create_output,
                # leave presets controls intact
            ],
        )

        # Create experiment
        create_btn.click(
            self._create_prompt_experiment,
            inputs=[
                name_input,
                description_input,
                data_file_input,
                max_iterations_input,
                llm_model_input,
                target_field_input,
                base_prompt_input,
                current_columns_state,
                task_type_input,
                preset_data_path_state,
            ],
            outputs=create_output,
        )

        # Local presets population
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

        # Initialize local preset buttons (best-effort)
        try:
            _init_local_presets()
        except Exception:
            pass

        # Prefill from a local preset
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
            # Prefill name if empty
            new_name = current_name or details.get("label") or label
            return (
                gr.update(value=new_name),
                gr.update(value=baseline_prompt),
                gr.update(visible=True, value=f"{ds_text}"),
            )

        # (Dynamic local preset buttons removed)

        # PromptEvo Examples prefill
        # (PromptEvo Examples dropdown prefill removed)

        # Helper: prefill and upload dataset for a fixed task name
        def _prefill_fixed_task(task_name):
            # Use local preset details derived from master_api/prompt_examples create.sh and files
            details = self.exp_manager.get_local_prompt_preset(task_name)
            if not details:
                return (
                    gr.update(), gr.update(), gr.update(), gr.update(),
                    "",  # preset_data_path_state
                    gr.update(visible=True, value=f"Error: failed to load example '{task_name}'")
                )

            # Upload preset dataset to storage to get data_path
            up = self.exp_manager.upload_local_prompt_preset_dataset(task_name)
            data_path = up.get("data_path", "") if isinstance(up, dict) else ""
            # Build public URL for MinIO object
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
                ds_msg = f"📁 Using preset dataset: {up.get('filename')}" if isinstance(up, dict) and "filename" in up else "📁 Preset dataset uploaded"

            label = details.get("label") or task_name.replace("_", " ").title()
            desc = details.get("task_description") or ""
            base_prompt = details.get("baseline_prompt") or ""
            target_field = details.get("target_field") or ""
            task_type_value = details.get("task_type") or "classification"
            vd = details.get("validation_defaults") or {}
            vt = vd.get("validation_type")
            bm = vd.get("binary_method")
            cm = vd.get("continuous_metric")

            return (
                gr.update(value=label),
                gr.update(value=desc),
                gr.update(value=base_prompt),
                gr.update(value=target_field, choices=[target_field]),
                gr.update(value=task_type_value),
                data_path,  # preset_data_path_state
                gr.update(value=ds_msg),
            )

        # Wire fixed task buttons
        for btn, task in [
            (btn_cmns, "commonsense"),
            (btn_emot, "emotion"),
            (btn_gsm8k, "gsm8k"),
            (btn_sent, "sentiment_analysis"),
            (btn_xsum, "xsum"),
        ]:
            # Bind task name via default argument to avoid missing-arg warnings
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
                    task_type_input,
                    preset_data_path_state,
                    dataset_info,
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
            # Add space if needed
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

        # Extract template placeholders
        template_pattern = r"\{([^}]+)\}"
        placeholders = re.findall(template_pattern, prompt)

        if not placeholders:
            return False, "Prompt must contain at least one template placeholder in {column_name} format"

        # Check if placeholders match available columns (excluding target)
        invalid_placeholders = []
        for placeholder in placeholders:
            if placeholder not in available_columns:
                invalid_placeholders.append(placeholder)

        if invalid_placeholders:
            return (
                False,
                f"Invalid template placeholders: {', '.join(invalid_placeholders)}. Available columns: {', '.join(available_columns)}",
            )

        # Ensure target column is included in templates or prompt makes sense
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
        target_field: str,
        base_prompt: str,
        available_columns: list,
        task_type: str,
        preset_data_path: str,
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
            task_type: Task type (TEMPORARY: classification, multi_choice, math, summarization)
            # FUTURE: Uncomment these validation criteria parameters when needed
            # validation_type: Validation type (Binary/Continuous)
            # binary_validation_method: Binary validation method
            # regexp_pattern: RegExp pattern for binary validation
            # continuous_metric: Continuous validation metric

        Returns:
            Status message
        """
        # Validate experiment name
        is_valid, error = validate_experiment_name(name)
        if not is_valid:
            return f"Error: {error}"

        # Validate max iterations
        is_valid, error = validate_max_iterations(max_iterations)
        if not is_valid:
            return f"Error: {error}"

        # Ensure available_columns populated for presets by downloading CSV from MinIO if needed
        try:
            if (not available_columns) and preset_data_path:
                # Use internal S3 URL to fetch CSV inside container
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
            # Best-effort; continue with possibly empty available_columns
            pass

        # Validate prompt template
        is_valid, error = self._validate_prompt_template(base_prompt, available_columns, target_field)
        if not is_valid:
            return f"Error: {error}"

        # Validate file upload unless preset data_path provided
        if not data_file and not preset_data_path:
            return "Error: Please upload a data file or choose a preset"

        src_path = extract_source_path_from_upload(data_file) if data_file else None
        if src_path:
            is_valid, error = validate_file_upload(src_path, [".csv", ".json", ".txt"])
            if not is_valid:
                return f"Error: {error}"

        # Validate target field
        if not target_field:
            return "Error: Please select a target column"

        try:
            # Handle data file upload
            data_path = preset_data_path or ""
            if not data_path and data_file is not None:
                src_path = extract_source_path_from_upload(data_file)

                if src_path and os.path.exists(src_path):
                    filename = os.path.basename(src_path)

                    # Upload file to S3 via Master API
                    upload_result = self.exp_manager.upload_data_file(src_path, filename)

                    if "error" in upload_result:
                        return f"Error uploading file to storage: {upload_result['error']}"

                    data_path = upload_result.get("data_path", "")
                    cleanup_temp_file(src_path)
                else:
                    return "Error: Uploaded file is not accessible"

            # TEMPORARY: Create prompt experiment payload with simplified task_type
            # FUTURE: Replace with the complex validation_criteria approach when needed
            prompt_experiment_data = {
                "name": name,
                "description": description,
                "data_path": data_path,
                "target_column": target_field,
                "base_prompt": base_prompt,
                "task_type": task_type,  # TEMPORARY: Simplified task type
                "llm_model": llm_model,
                "max_iterations": max_iterations,
            }

            # FUTURE: Uncomment this complex validation criteria approach when needed
            """
            # Create prompt experiment payload matching PromptExperimentCreate schema
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
                "max_iterations": max_iterations,
            }
            """

            # Create prompt experiment using new endpoint
            result = self.exp_manager.create_prompt_experiment(prompt_experiment_data)

            if "error" in result:
                return f"Error: {result['error']}"

            return f"✅ Prompt experiment '{name}' created successfully with ID: {result['id']}\nFile uploaded to storage: {data_path}\nTarget: {target_field}\nTask type: {task_type}\nTemplate placeholders: {len(re.findall(r'\{([^}]+)\}', base_prompt))}"

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
