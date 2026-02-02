import os
import json
import tempfile
from typing import Optional

import gradio as gr
import requests
from config.settings import (
    INTERNAL_S3_API_URL,
    STORAGE_BUCKET_NAME,
    VALIDATION_RULES,
)
from loguru import logger
from utils.file_handlers import (
    cleanup_temp_file,
    count_csv_rows,
    download_preset_dataset,
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
    get_llm_model_choices,
)

from .base import BaseComponent


class CreateChainExperimentComponent(BaseComponent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bucket_name = None

    def build(self) -> gr.Column:
        with gr.Column() as component:
            gr.Markdown("## Create Chain Reasoning Experiment")
            gr.Markdown("#### Start with a Preset Example")
            with gr.Row():
                btn_gsm8k = gr.Button("GSM8K Chain", size="sm")
                btn_commonsense = gr.Button("Commonsense QA", size="sm")
                btn_sentiment = gr.Button("Sentiment Analysis", size="sm")
                btn_emotion = gr.Button("Emotion Classification", size="sm")

            with gr.Row():
                with gr.Column(scale=1):
                    name_input = gr.Textbox(
                        label="Experiment Name",
                        placeholder="Enter experiment name...",
                    )
                    description_input = gr.Textbox(
                        label="Description",
                        placeholder="Optional description of your chain experiment...",
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
                    gr.Markdown("### Chain Configuration (JSON)")

                    with gr.Accordion("📝 Chain Configuration Examples", open=False):
                        gr.Markdown(
                            """
                            **Chain Config Format:**
                            ```json
                            {
                              "steps": [
                                {
                                  "number": 1,
                                  "title": "Problem Understanding",
                                  "aim": "Understand the problem",
                                  "reasoning_questions": "What is being asked?",
                                  "dependencies": [],
                                  "step_context_queries": ["problem"],
                                  "stage_action": "Read and analyze the problem",
                                  "example_reasoning": "First, I need to understand..."
                                }
                              ],
                              "search_config": {
                                "strategy": "substring"
                              }
                            }
                            ```
                            """
                        )

                    base_chain_config_input = gr.Code(
                        label="Base Chain Config",
                        value='{"steps": []}',
                        language="json",
                        lines=15,
                    )
                    gr.Markdown("*Enter your CARL chain configuration as JSON*")
                    generate_baseline_btn = gr.Button(
                        "Generate Baseline Chain", size="sm", variant="secondary"
                    )
                    
                    evolution_mode_input = gr.Radio(
                        choices=["full_chain", "single_step"],
                        value="full_chain",
                        label="Evolution Mode",
                        info="Evolve entire chain or focus on a single step",
                    )
                    step_number_input = gr.Number(
                        value=None,
                        label="Step Number (for single step evolution)",
                        info="Step number to evolve (1-based). Leave empty if evolving full chain.",
                        visible=False,
                        precision=0,
                        minimum=1,
                    )

            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("#### 🔧 Validation Criteria")
                    with gr.Group():
                        validation_type_input = gr.Dropdown(
                            choices=["Binary (0/1)", "Continuous (0..1)"],
                            label="Validation Type",
                            info="Select how chain outputs should be validated",
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
            preset_data_path_state = gr.State(value="")
            max_dataset_size_state = gr.State(value=None)
            preset_target_field_state = gr.State(value=None)

            self._setup_event_handlers(
                data_file_input,
                target_field_input,
                dataset_info,
                name_input,
                description_input,
                max_iterations_input,
                llm_model_input,
                base_chain_config_input,
                generate_baseline_btn,
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
                dataset_size_input,
                test_size_input,
                split_info,
                max_dataset_size_state,
                current_columns_state,
                btn_gsm8k,
                btn_commonsense,
                btn_sentiment,
                btn_emotion,
                preset_target_field_state,
                evolution_mode_input,
                step_number_input,
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
            base_chain_config_input,
            generate_baseline_btn,
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
            dataset_size_input,
            test_size_input,
            split_info,
            max_dataset_size_state,
            current_columns_state,
                btn_gsm8k,
                btn_commonsense,
                btn_sentiment,
                btn_emotion,
                preset_target_field_state,
                evolution_mode_input,
                step_number_input,
            ) = inputs

        def _update_file_and_columns(file, preset_target_field):
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

            # Prefer preset target field when available, otherwise fallback
            if preset_target_field and preset_target_field in target_choices:
                target_value = preset_target_field
            elif "target" in target_choices:
                target_value = "target"
            elif target_choices:
                target_value = target_choices[0]
            else:
                target_value = None

            target_update = gr.update(
                choices=target_choices,
                value=target_value,
                visible=bool(target_choices),
            )

            # Clear preset target field after applying it once
            preset_target_field_update = gr.update(value=None)

            return (
                dataset_info_update,
                target_update,
                file_columns,
                dataset_size_update,
                new_max_size,
                preset_target_field_update,
            )

        data_file_input.change(
            _update_file_and_columns,
            inputs=[data_file_input, preset_target_field_state],
            outputs=[
                dataset_info,
                target_field_input,
                current_columns_state,
                dataset_size_input,
                max_dataset_size_state,
                preset_target_field_state,
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

        def _generate_baseline_chain(all_columns, target_column):
            """Generate a baseline chain configuration."""
            if not all_columns or not target_column:
                return gr.update(value='{"steps": []}')

            baseline_chain = {
                "steps": [
                    {
                        "number": 1,
                        "title": "Problem Understanding",
                        "aim": "Understand the problem and identify what needs to be computed",
                        "reasoning_questions": "What is the question asking? What information is provided?",
                        "dependencies": [],
                        "step_context_queries": [col for col in all_columns if col != target_column],
                        "stage_action": "Read the problem carefully, identify the question being asked, and list all given information",
                        "example_reasoning": "First, I need to understand what the problem is asking. I'll identify the key information provided.",
                    },
                    {
                        "number": 2,
                        "title": "Solution Computation",
                        "aim": "Perform the necessary operations to solve the problem step by step",
                        "reasoning_questions": "What operations are needed? What is the correct sequence?",
                        "dependencies": [1],
                        "step_context_queries": [col for col in all_columns if col != target_column],
                        "stage_action": "Break down the problem into smaller steps, compute intermediate values, and derive the final answer",
                        "example_reasoning": "Now I'll perform the necessary operations. I'll work through each step systematically and arrive at the final answer.",
                    },
                ],
                "max_workers": 2,
                "enable_progress": False,
                "search_config": {"strategy": "substring"},
            }

            return gr.update(value=json.dumps(baseline_chain, indent=2))

        generate_baseline_btn.click(
            _generate_baseline_chain,
            inputs=[current_columns_state, target_field_input],
            outputs=[base_chain_config_input],
        )
        
        def _on_evolution_mode_change(mode):
            return gr.update(visible=(mode == "single_step"))
        
        evolution_mode_input.change(
            _on_evolution_mode_change,
            inputs=[evolution_mode_input],
            outputs=[step_number_input],
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
                gr.update(value='{"steps": []}'),
                gr.update(value="Binary (0/1)"),
                gr.update(visible=True),
                gr.update(value="equality", visible=True),
                gr.update(value="", visible=False),
                gr.update(visible=False),
                gr.update(value=None, visible=False),
                gr.update(value=None),
                gr.update(value=0.2),
                gr.update(value="", visible=False),
                gr.update(value=None),
                [],
                "",
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
                base_chain_config_input,
                validation_type_input,
                binary_validation_group,
                binary_validation_method_input,
                regexp_pattern_input,
                continuous_validation_group,
                continuous_metric_input,
                dataset_size_input,
                test_size_input,
                split_info,
                max_dataset_size_state,
                current_columns_state,
                preset_data_path_state,
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

        create_btn.click(
            self._create_chain_experiment,
            inputs=[
                name_input,
                description_input,
                data_file_input,
                max_iterations_input,
                llm_model_input,
                target_field_input,
                base_chain_config_input,
                current_columns_state,
                validation_type_input,
                binary_validation_method_input,
                regexp_pattern_input,
                continuous_metric_input,
                preset_data_path_state,
                dataset_size_input,
                test_size_input,
                evolution_mode_input,
                step_number_input,
            ],
            outputs=create_output,
        )

        def _generate_chain_config_for_task(task_type: str, task_description: str) -> dict:
            """Generate chain configuration based on task type."""
            if task_type in ["math", "gsm8k"]:
                return {
                    "steps": [
                        {
                            "number": 1,
                            "title": "Problem Understanding",
                            "aim": "Understand the math word problem and identify what needs to be computed",
                            "reasoning_questions": "What is the question asking? What quantities are given in the problem? What mathematical operations are needed?",
                            "dependencies": [],
                            "step_context_queries": ["problem"],
                            "stage_action": "Read the problem carefully, identify the question being asked, and list all given quantities and their relationships",
                            "example_reasoning": "First, I need to understand what the problem is asking. I'll identify the key information: what quantities are provided, what relationships exist between them, and what final answer is required.",
                        },
                        {
                            "number": 2,
                            "title": "Solution Computation",
                            "aim": "Perform the necessary calculations to solve the problem step by step",
                            "reasoning_questions": "What intermediate calculations are needed? What is the correct sequence of operations? How do I combine the given quantities?",
                            "dependencies": [1],
                            "step_context_queries": ["problem"],
                            "stage_action": "Break down the problem into smaller subproblems, compute intermediate values step by step, and derive the final numeric answer",
                            "example_reasoning": "Now I'll perform the calculations. I'll work through each step systematically, showing my reasoning, and arrive at the final numeric answer.",
                        },
                    ],
                    "max_workers": 2,
                    "enable_progress": False,
                    "search_config": {"strategy": "substring"},
                }
            elif task_type in ["multi_choice", "commonsense", "emotion"]:
                return {
                    "steps": [
                        {
                            "number": 1,
                            "title": "Question Analysis",
                            "aim": "Understand the question and identify key information",
                            "reasoning_questions": "What is the question asking? What information is provided? What are the available options?",
                            "dependencies": [],
                            "step_context_queries": ["question", "options"],
                            "stage_action": "Read the question carefully, identify what is being asked, and review all available options",
                            "example_reasoning": "First, I need to understand the question. I'll identify what is being asked and review all the available options to understand the choices.",
                        },
                        {
                            "number": 2,
                            "title": "Reasoning and Selection",
                            "aim": "Apply reasoning to select the correct answer from the options",
                            "reasoning_questions": "Which option best answers the question? What reasoning supports this choice?",
                            "dependencies": [1],
                            "step_context_queries": ["question", "options"],
                            "stage_action": "Apply logical reasoning to evaluate each option and select the most appropriate answer",
                            "example_reasoning": "Now I'll evaluate each option carefully, apply the relevant reasoning, and select the answer that best fits the question.",
                        },
                    ],
                    "max_workers": 2,
                    "enable_progress": False,
                    "search_config": {"strategy": "substring"},
                }
            elif task_type in ["classification", "sentiment_analysis"]:
                return {
                    "steps": [
                        {
                            "number": 1,
                            "title": "Text Analysis",
                            "aim": "Analyze the input text to understand its content and key features",
                            "reasoning_questions": "What is the main content of the text? What are the key features or indicators?",
                            "dependencies": [],
                            "step_context_queries": ["text"],
                            "stage_action": "Read and analyze the text carefully, identifying key features, tone, and content",
                            "example_reasoning": "First, I need to analyze the text. I'll identify the key features, tone, and content that are relevant for classification.",
                        },
                        {
                            "number": 2,
                            "title": "Classification",
                            "aim": "Classify the text based on the analysis",
                            "reasoning_questions": "What category does this text belong to? What evidence supports this classification?",
                            "dependencies": [1],
                            "step_context_queries": ["text"],
                            "stage_action": "Based on the analysis, determine the appropriate classification and provide the answer",
                            "example_reasoning": "Now I'll classify the text based on my analysis. I'll consider the evidence and provide the appropriate classification.",
                        },
                    ],
                    "max_workers": 2,
                    "enable_progress": False,
                    "search_config": {"strategy": "substring"},
                }
            else:
                return {
                    "steps": [
                        {
                            "number": 1,
                            "title": "Problem Understanding",
                            "aim": "Understand the problem and identify what needs to be computed",
                            "reasoning_questions": "What is the question asking? What information is provided?",
                            "dependencies": [],
                            "step_context_queries": ["*"],
                            "stage_action": "Read the problem carefully, identify the question being asked, and list all given information",
                            "example_reasoning": "First, I need to understand what the problem is asking. I'll identify the key information provided.",
                        },
                        {
                            "number": 2,
                            "title": "Solution Computation",
                            "aim": "Perform the necessary operations to solve the problem step by step",
                            "reasoning_questions": "What operations are needed? What is the correct sequence?",
                            "dependencies": [1],
                            "step_context_queries": ["*"],
                            "stage_action": "Break down the problem into smaller steps, compute intermediate values, and derive the final answer",
                            "example_reasoning": "Now I'll perform the necessary operations. I'll work through each step systematically and arrive at the final answer.",
                        },
                    ],
                    "max_workers": 2,
                    "enable_progress": False,
                    "search_config": {"strategy": "substring"},
                }

        def _prefill_from_preset(preset_name: str, display_name: str, task_type: str, task_description: str):
            """Prefill form with preset configuration and load dataset from database."""
            chain_config = _generate_chain_config_for_task(task_type, task_description)

            dataset_info_update = gr.update(value="No dataset selected")
            file_update = gr.update(value=None)
            target_update = gr.update(choices=[], value=None, visible=False)
            dataset_size_update = gr.update(value=None)
            new_max_size = None
            columns_update = []
            
            # Get preset configuration to determine correct target field
            preset_target_field = "target"  # Default fallback
            try:
                preset_info = self.exp_manager.get_local_prompt_preset(preset_name)
                if preset_info and preset_info.get("target_field"):
                    preset_target_field = preset_info.get("target_field")
                    logger.info(f"Using target_field '{preset_target_field}' from preset '{preset_name}' configuration")
            except Exception as e:
                logger.debug(f"Could not get preset configuration for {preset_name}: {e}, using default 'target'")

            try:
                if not self.bucket_name:
                    self.bucket_name = self.status_service.get_storage_status()

                bucket_name = self.bucket_name or STORAGE_BUCKET_NAME

                if bucket_name:
                    dataset_paths = [f"prompt_data/{preset_name}/train.csv", f"data/{preset_name}/train.csv"]
                    local_path = None
                    
                    for ds_path in dataset_paths:
                        logger.debug(f"Trying to download {preset_name} dataset from: {ds_path} (bucket: {bucket_name})")
                        local_path = download_preset_dataset(ds_path, bucket_name)
                        if local_path and os.path.exists(local_path):
                            logger.info(f"Successfully downloaded {preset_name} dataset to: {local_path}")
                            break
                    
                    if local_path and os.path.exists(local_path):
                        file_update = gr.update(value=local_path)
                        filename = os.path.basename(local_path)
                        dataset_info_update = gr.update(value=f"📁 Using preset dataset: {filename}")

                        try:
                            file_columns = read_csv_columns(local_path)
                            columns_update = file_columns
                            target_choices = get_default_target_choices("classification", file_columns)

                            # Use preset target_field if available, otherwise fallback to "target" or first column
                            if preset_target_field in target_choices:
                                target_value = preset_target_field
                            elif "target" in target_choices:
                                target_value = "target"
                            elif target_choices:
                                target_value = target_choices[0]
                            else:
                                target_value = None

                            target_update = gr.update(
                                choices=target_choices,
                                value=target_value,
                                visible=bool(target_choices),
                            )

                            row_count = count_csv_rows(local_path)
                            if row_count is not None and row_count > 0:
                                logger.info(f"Counted {row_count} rows in {preset_name} dataset: {local_path}")
                                dataset_size_update = gr.update(value=row_count)
                                new_max_size = row_count
                        except Exception as e:
                            logger.warning(f"Failed to process {preset_name} dataset {local_path}: {e}")
                    else:
                        logger.warning(f"Failed to download {preset_name} dataset from any of the paths: {dataset_paths}")
                        dataset_info_update = gr.update(value="⚠️ Failed to load preset dataset")
                else:
                    logger.error(f"No bucket name available for downloading {preset_name} dataset")
            except Exception as e:
                logger.error(f"Error loading {preset_name} preset dataset: {e}", exc_info=True)
                dataset_info_update = gr.update(value="⚠️ Error loading preset dataset")

            return (
                gr.update(value=display_name),
                gr.update(value=task_description),
                file_update,
                dataset_info_update,
                target_update,
                gr.update(value=json.dumps(chain_config, indent=2)),
                dataset_size_update,
                new_max_size,
                columns_update,
                gr.update(value=preset_target_field),
            )

        btn_gsm8k.click(
            lambda: _prefill_from_preset("gsm8k", "GSM8K Chain Evolution", "math", "Evolve reasoning chains for GSM8K math word problems"),
            outputs=[
                name_input,
                description_input,
                data_file_input,
                dataset_info,
                target_field_input,
                base_chain_config_input,
                dataset_size_input,
                max_dataset_size_state,
                current_columns_state,
                preset_target_field_state,
            ],
        )

        btn_commonsense.click(
            lambda: _prefill_from_preset("commonsense", "Commonsense QA Chain Evolution", "multi_choice", "Evolve reasoning chains for commonsense question answering"),
            outputs=[
                name_input,
                description_input,
                data_file_input,
                dataset_info,
                target_field_input,
                base_chain_config_input,
                dataset_size_input,
                max_dataset_size_state,
                current_columns_state,
                preset_target_field_state,
            ],
        )

        btn_sentiment.click(
            lambda: _prefill_from_preset("sentiment_analysis", "Sentiment Analysis Chain Evolution", "classification", "Evolve reasoning chains for sentiment classification"),
            outputs=[
                name_input,
                description_input,
                data_file_input,
                dataset_info,
                target_field_input,
                base_chain_config_input,
                dataset_size_input,
                max_dataset_size_state,
                current_columns_state,
                preset_target_field_state,
            ],
        )

        btn_emotion.click(
            lambda: _prefill_from_preset("emotion", "Emotion Classification Chain Evolution", "multi_choice", "Evolve reasoning chains for emotion classification"),
            outputs=[
                name_input,
                description_input,
                data_file_input,
                dataset_info,
                target_field_input,
                base_chain_config_input,
                dataset_size_input,
                max_dataset_size_state,
                current_columns_state,
                preset_target_field_state,
            ],
        )

    def _calculate_timeout_from_iterations(self, max_iterations: int) -> int:
        if max_iterations <= 0:
            return 3600
        
        seconds_per_iteration = 90
        base_timeout = max_iterations * seconds_per_iteration
        timeout_with_buffer = int(base_timeout * 1.3)
        return max(3600, timeout_with_buffer)

    def _validate_chain_config(self, chain_config_json: str) -> tuple[bool, str]:
        if not chain_config_json or not chain_config_json.strip():
            return False, "Chain configuration cannot be empty"

        try:
            chain_config = json.loads(chain_config_json)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON: {str(e)}"

        if "steps" not in chain_config:
            return False, "Chain config must contain a 'steps' array"

        if not isinstance(chain_config["steps"], list):
            return False, "Chain config 'steps' must be a list"

        if len(chain_config["steps"]) == 0:
            return False, "Chain config must contain at least one step"

        required_fields = ["number", "title", "aim", "reasoning_questions", "dependencies", "step_context_queries", "stage_action", "example_reasoning"]
        for i, step in enumerate(chain_config["steps"]):
            if not isinstance(step, dict):
                return False, f"Step {i + 1} must be a dictionary"

            for field in required_fields:
                if field not in step:
                    return False, f"Step {i + 1} is missing required field: {field}"

        return True, ""

    def _create_chain_experiment(
        self,
        name: str,
        description: str,
        data_file,
        max_iterations: int,
        llm_model: str,
        target_field: str,
        base_chain_config: str,
        available_columns: list,
        validation_type: str,
        binary_validation_method: str,
        regexp_pattern: str,
        continuous_metric: str,
        preset_data_path: str,
        dataset_size: Optional[float],
        test_size: float,
        evolution_mode: str,
        step_number: Optional[float],
    ) -> str:
        is_valid, error = validate_experiment_name(name)
        if not is_valid:
            return f"Error: {error}"

        is_valid, error = validate_max_iterations(max_iterations)
        if not is_valid:
            return f"Error: {error}"

        is_valid, error = self._validate_chain_config(base_chain_config)
        if not is_valid:
            return f"Error: {error}"

        if binary_validation_method == "regexp":
            is_valid, error = validate_regexp_pattern(regexp_pattern)
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

            # Calculate timeout based on max_iterations
            calculated_timeout = self._calculate_timeout_from_iterations(max_iterations)
            
            evolution_mode = evolution_mode or "full_chain"
            step_number_int = None
            if evolution_mode == "single_step" and step_number is not None:
                step_number_int = int(step_number)
                try:
                    chain_config = json.loads(base_chain_config)
                    steps = chain_config.get("steps", [])
                    if step_number_int < 1 or step_number_int > len(steps):
                        return f"Error: Step number {step_number_int} is out of range. Chain has {len(steps)} steps."
                except json.JSONDecodeError:
                    return "Error: Invalid chain configuration JSON"
            
            chain_experiment_data = {
                "name": name,
                "description": description,
                "data_path": data_path,
                "target_column": target_field,
                "base_chain_config": base_chain_config,
                "validation_criteria": validation_criteria,
                "llm_model": llm_model,
                "max_iterations": max_iterations,
                "timeout_seconds": calculated_timeout,
                "evolution_mode": evolution_mode,
                "step_number": step_number_int,
            }

            if dataset_size is not None and dataset_size > 0:
                chain_experiment_data["dataset_size"] = int(dataset_size)
            if test_size is not None:
                chain_experiment_data["test_size"] = float(test_size)

            result = self.exp_manager.create_chain_experiment(chain_experiment_data)

            if "error" in result:
                return f"Error: {result['error']}"

            timeout_hours = calculated_timeout / 3600
            return (
                f"✅ Chain experiment '{name}' created successfully with ID: {result['id']}\n"
                f"File uploaded to storage: {data_path}\n"
                f"Target: {target_field}\n"
                f"Chain steps: {len(json.loads(base_chain_config).get('steps', []))}\n"
                f"Max iterations: {max_iterations}\n"
                f"Timeout: {calculated_timeout}s ({timeout_hours:.1f} hours)"
            )

        except Exception as e:
            logger.error(f"Error creating chain experiment: {e}")
            return f"Error creating chain experiment: {str(e)}"
