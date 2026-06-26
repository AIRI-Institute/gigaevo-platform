"""Create Experiment tab component."""

import os
import tempfile
from typing import Optional

import gradio as gr
import requests
from config.settings import (
    INTERNAL_S3_API_URL,
    STORAGE_BUCKET_NAME,
    TASK_TYPES,
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
from utils.ui_helpers import update_dropdown_choices
from utils.validators import (
    get_default_target_choices,
    validate_experiment_config,
    validate_experiment_name,
    validate_file_upload,
    validate_max_iterations,
)

from common.llm_registry import get_default_llm_model_id, get_llm_model_choices

from .base import BaseComponent


class CreateExperimentComponent(BaseComponent):
    """Component for creating new experiments."""

    def __init__(self, *args, **kwargs):
        """Initialize create experiment component."""
        super().__init__(*args, **kwargs)
        self.bucket_name = None

    def build(self) -> gr.Column:
        """Build the create experiment tab.

        Returns:
            Gradio Column component
        """
        with gr.Column() as component:
            gr.Markdown("## Create New Experiment")

            # Preset Example Selection (moved above the form)
            with gr.Row():
                with gr.Column():
                    gr.Markdown("### Start with a Preset Example")
                    with gr.Row():
                        preset_btn_1 = gr.Button(
                            "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                        )
                        preset_btn_2 = gr.Button(
                            "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                        )
                        preset_btn_3 = gr.Button(
                            "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                        )
                        preset_btn_4 = gr.Button(
                            "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                        )
                        preset_btn_5 = gr.Button(
                            "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                        )
                        preset_btn_6 = gr.Button(
                            "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                        )
                        preset_btn_7 = gr.Button(
                            "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                        )
                        preset_btn_8 = gr.Button(
                            "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                        )
                    preset_active_state = gr.State(value=None)
                    preset_target_state = gr.State(value=None)
                    max_dataset_size_state = gr.State(value=None)  # Store maximum dataset size

            gr.Markdown("---")  # Separator line
            gr.Markdown("### Or Create Custom Experiment")

            with gr.Row():
                with gr.Column():
                    name_input = gr.Textbox(label="Experiment Name", placeholder="Enter experiment name...")
                    description_input = gr.Textbox(label="Description", placeholder="Optional description...", lines=3)
                    data_file_input = gr.File(label="Data File", file_types=[".csv", ".json", ".txt"])
                    dataset_info = gr.Textbox(
                        label="Dataset Source",
                        value="No dataset selected",
                        interactive=False,
                        info="Current dataset being used for the experiment",
                    )
                    task_type_input = gr.Dropdown(
                        label="Task Type",
                        choices=TASK_TYPES,
                        value=None,
                        interactive=True,
                    )
                    model_type_input = gr.Dropdown(
                        label="Model Type",
                        choices=[],
                        value=None,
                        interactive=True,
                        visible=False,
                    )
                    target_field_input = gr.Dropdown(
                        label="Target Column",
                        choices=[],
                        interactive=True,
                        value=None,
                        visible=False,
                    )
                    num_classes_input = gr.Textbox(
                        label="Number of Classes",
                        placeholder="e.g., 2 (optional; fill either Target Column or this)",
                        visible=False,
                    )
                    num_clusters_input = gr.Textbox(
                        label="Number of Clusters",
                        placeholder="e.g., 3 (defaults to 3 if empty)",
                        visible=False,
                    )
                    clusters_hint = gr.Markdown(
                        "If left empty, the number of clusters defaults to 3.",
                        visible=False,
                    )

                with gr.Column():
                    max_iterations_input = gr.Slider(
                        minimum=VALIDATION_RULES["max_iterations"]["min"],
                        maximum=VALIDATION_RULES["max_iterations"]["max"],
                        value=100,
                        label="Max Iterations",
                    )
                    llm_model_input = gr.Dropdown(
                        choices=get_llm_model_choices(),  # (label, id)
                        value=get_default_llm_model_id(),  # id
                        label="LLM Model",
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
                    spec_preview_btn = gr.Button("Preview Spec JSON")
                    spec_preview_output = gr.Code(label="Spec JSON", language="json")

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
                create_btn = gr.Button("Create Experiment", variant="primary")
                clean_btn = gr.Button("🧹 Clean Form", variant="secondary")
            create_output = gr.Textbox(label="Status", interactive=False)

            # Wire up event handlers
            self._setup_event_handlers(
                data_file_input,
                task_type_input,
                target_field_input,
                num_classes_input,
                num_clusters_input,
                clusters_hint,
                preset_active_state,
                preset_target_state,
                dataset_info,
                spec_preview_btn,
                spec_preview_output,
                clean_btn,
                create_btn,
                create_output,
                name_input,
                description_input,
                max_iterations_input,
                model_type_input,
                llm_model_input,
                dataset_size_input,
                test_size_input,
                split_info,
                max_dataset_size_state,
                preset_btn_1,
                preset_btn_2,
                preset_btn_3,
                preset_btn_4,
                preset_btn_5,
                preset_btn_6,
                preset_btn_7,
                preset_btn_8,
                enable_memory_checkbox,
                memory_namespace_input,
            )

        return component

    def _setup_event_handlers(self, *inputs):
        """Set up all event handlers for the component."""
        (
            data_file_input,
            task_type_input,
            target_field_input,
            num_classes_input,
            num_clusters_input,
            clusters_hint,
            preset_active_state,
            preset_target_state,
            dataset_info,
            spec_preview_btn,
            spec_preview_output,
            clean_btn,
            create_btn,
            create_output,
            name_input,
            description_input,
            max_iterations_input,
            model_type_input,
            llm_model_input,
            dataset_size_input,
            test_size_input,
            split_info,
            max_dataset_size_state,
            *preset_buttons_and_mem,
        ) = inputs

        enable_memory_checkbox = preset_buttons_and_mem[-2]
        memory_namespace_input = preset_buttons_and_mem[-1]
        preset_buttons_debug = preset_buttons_and_mem[:-2]
        preset_buttons = preset_buttons_debug

        # Update target choices when file changes
        def _update_target_choices(file, task_type, preset_active, preset_target, max_size_state):
            src_path = extract_source_path_from_upload(file)
            file_columns = read_csv_columns(src_path) if src_path else []

            # Update dataset info when user uploads a file
            dataset_info_update = gr.update()
            dataset_size_update = gr.update(value=None)
            new_max_size = None

            if src_path:
                filename = os.path.basename(src_path)
                dataset_info_update = gr.update(value=f"📁 Using uploaded file: {filename}")

                # Count rows in uploaded file and set as maximum
                try:
                    row_count = count_csv_rows(src_path)
                    if row_count is not None and row_count > 0:
                        logger.info(f"Counted {row_count} rows in uploaded file: {src_path}")
                        # Set maximum value and current value to total rows
                        dataset_size_update = gr.update(value=row_count)
                        new_max_size = row_count
                except Exception as e:
                    logger.warning(f"Failed to count rows in uploaded file {src_path}: {e}")

            visible, choices, value = update_dropdown_choices(src_path, task_type, preset_target, file_columns)
            return (
                gr.update(choices=choices, value=value, visible=visible),
                dataset_info_update,
                dataset_size_update,
                new_max_size,  # max_dataset_size_state
            )

        data_file_input.change(
            _update_target_choices,
            inputs=[data_file_input, task_type_input, preset_active_state, preset_target_state, max_dataset_size_state],
            outputs=[target_field_input, dataset_info, dataset_size_input, max_dataset_size_state],
        )

        # Update split info when dataset size or test size changes
        def _update_split_info(dataset_size, test_size, data_file, max_size):
            # Handle None, empty string, or zero values
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

                # Calculate minimum (10% of max_size) and maximum allowed
                min_size = None
                if max_size is not None and max_size > 0:
                    min_size = max(1, int(max_size * 0.1))  # At least 10%, minimum 1 row

                    # Validate that dataset_size doesn't exceed maximum
                    if dataset_size > max_size:
                        dataset_size = max_size
                        logger.warning(f"Dataset size exceeds maximum {max_size}, using {max_size}")

                    # Validate that dataset_size is at least 10% of maximum
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

            # Also return corrected dataset_size if validation changed it
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

        # Show/hide inputs depending on task type
        def _normalize_task_type(task_type: Optional[str]) -> Optional[str]:
            if isinstance(task_type, str) and task_type.endswith("_automl"):
                return task_type.replace("_automl", "")
            return task_type

        def _on_task_type_change(task_type, file, preset_active, preset_target):
            base_type = _normalize_task_type(task_type)
            src_path = extract_source_path_from_upload(file)
            file_columns = read_csv_columns(src_path) if src_path else []

            # Handle preset or file-based target field
            if preset_active:
                visible, choices, value = update_dropdown_choices(src_path, base_type, preset_target, [])
                if base_type == "classification":
                    target_update = gr.update(choices=choices, visible=True, value=value)
                    num_classes_update = gr.update(visible=True, value="")
                    num_clusters_update = gr.update(visible=False, value="")
                    clusters_hint_update = gr.update(visible=False)
                elif base_type == "regression":
                    target_update = gr.update(choices=choices, visible=True, value=value)
                    num_classes_update = gr.update(visible=False, value="")
                    num_clusters_update = gr.update(visible=False, value="")
                    clusters_hint_update = gr.update(visible=False)
                elif base_type == "clustering":
                    target_update = gr.update(visible=False, value=None)
                    num_classes_update = gr.update(visible=False, value="")
                    num_clusters_update = gr.update(visible=True, value="3")
                    clusters_hint_update = gr.update(visible=True)
                else:
                    target_update = gr.update(visible=False, value=None)
                    num_classes_update = gr.update(visible=False, value="")
                    num_clusters_update = gr.update(visible=False, value="")
                    clusters_hint_update = gr.update(visible=False)
            else:
                choices = get_default_target_choices(base_type, file_columns)
                if base_type == "classification":
                    target_update = gr.update(choices=choices, visible=True, value=choices[0] if choices else None)
                    num_classes_update = gr.update(visible=True, value="")
                    num_clusters_update = gr.update(visible=False, value="")
                    clusters_hint_update = gr.update(visible=False)
                elif base_type == "regression":
                    target_update = gr.update(choices=choices, visible=True, value=choices[0] if choices else None)
                    num_classes_update = gr.update(visible=False, value="")
                    num_clusters_update = gr.update(visible=False, value="")
                    clusters_hint_update = gr.update(visible=False)
                elif base_type == "clustering":
                    target_update = gr.update(visible=False, value=None)
                    num_classes_update = gr.update(visible=False, value="")
                    num_clusters_update = gr.update(visible=True, value="3")
                    clusters_hint_update = gr.update(visible=True)
                else:
                    target_update = gr.update(visible=False, value=None)
                    num_classes_update = gr.update(visible=False)
                    num_clusters_update = gr.update(visible=False)
                    clusters_hint_update = gr.update(visible=False)

            # Configure model type choices based on base task type
            if base_type == "regression":
                model_choices = ["Ridge", "LightAutoML"]
                model_default = "Ridge"
            elif base_type == "classification":
                model_choices = ["LogisticRegression", "LightAutoML", "CatBoost"]
                model_default = "LogisticRegression"
            else:
                model_choices = []
                model_default = None

            model_update = gr.update(
                choices=model_choices,
                value=model_default,
                visible=bool(model_choices),
            )

            return (
                target_update,
                num_classes_update,
                num_clusters_update,
                clusters_hint_update,
                model_update,
            )

            # Default case
            return (
                gr.update(visible=False, value=None),
                gr.update(visible=False),
                gr.update(visible=False),
                gr.update(visible=False),
            )

        task_type_input.change(
            _on_task_type_change,
            inputs=[task_type_input, data_file_input, preset_active_state, preset_target_state],
            outputs=[target_field_input, num_classes_input, num_clusters_input, clusters_hint, model_type_input],
        )

        # Spec preview
        spec_preview_btn.click(
            self.build_spec_preview,
            inputs=[
                description_input,
                task_type_input,
                target_field_input,
                num_classes_input,
                num_clusters_input,
                data_file_input,
                preset_active_state,
            ],
            outputs=spec_preview_output,
        )

        # Clean form
        def _clean_form():
            return (
                gr.update(value=""),  # name_input
                gr.update(value=""),  # description_input
                gr.update(value=None),  # data_file_input
                gr.update(value=None),  # task_type_input
                gr.update(choices=[], value=None, visible=False),  # target_field_input
                gr.update(visible=False, value=""),  # num_classes_input
                gr.update(visible=False, value=""),  # num_clusters_input
                gr.update(visible=False),  # clusters_hint
                gr.update(value="No dataset selected"),  # dataset_info
                None,  # preset_active_state
                None,  # preset_target_state
                gr.update(value=""),  # spec_preview_output
                gr.update(choices=[], value=None, visible=False),  # model_type_input
                gr.update(value=None),  # dataset_size_input
                gr.update(value=0.2),  # test_size_input
                gr.update(value="", visible=False),  # split_info
                None,  # max_dataset_size_state
            )

        clean_btn.click(
            _clean_form,
            outputs=[
                name_input,
                description_input,
                data_file_input,
                task_type_input,
                target_field_input,
                num_classes_input,
                num_clusters_input,
                clusters_hint,
                dataset_info,
                preset_active_state,
                preset_target_state,
                spec_preview_output,
                model_type_input,
                dataset_size_input,
                test_size_input,
                split_info,
                max_dataset_size_state,
            ],
        )

        # Create experiment
        create_btn.click(
            self._create_experiment,
            inputs=[
                name_input,
                description_input,
                data_file_input,
                max_iterations_input,
                llm_model_input,
                task_type_input,
                target_field_input,
                num_classes_input,
                num_clusters_input,
                model_type_input,
                preset_active_state,
                dataset_size_input,
                test_size_input,
                enable_memory_checkbox,
                memory_namespace_input,
            ],
            outputs=create_output,
        )

        # Load preset buttons and get example names for click handlers
        examples = self.exp_manager.list_examples()
        example_names = [example.get("name", "") for example in examples if example.get("name")]

        # Apply button updates and set up click handlers
        updates = self._load_preset_buttons(*preset_buttons)
        if updates and len(updates) == len(preset_buttons):
            for i, (btn, update) in enumerate(zip(preset_buttons, updates)):
                # Apply update to button - handle dict vs object case
                if isinstance(update, dict):
                    if "value" in update and update["value"]:
                        btn.value = update["value"]
                    if "visible" in update:
                        btn.visible = update["visible"]
                    if "interactive" in update:
                        btn.interactive = update["interactive"]
                else:
                    # Handle Gradio update object
                    if hasattr(update, "value") and update.value:
                        btn.value = update.value
                    if hasattr(update, "visible"):
                        btn.visible = update.visible
                    if hasattr(update, "interactive"):
                        btn.interactive = update.interactive

                # Set up click handler if button has an example
                if i < len(example_names) and example_names[i]:
                    logger.info(f"Assigning preset '{example_names[i]}' to button {i + 1}")

                    # Create closure to capture the correct example name
                    def create_click_handler(example_name):
                        def handler():
                            return self._on_preset_button_click(example_name)

                        return handler

                    btn.click(
                        create_click_handler(example_names[i]),
                        outputs=[
                            description_input,
                            task_type_input,
                            target_field_input,
                            num_classes_input,
                            num_clusters_input,
                            clusters_hint,
                            name_input,
                            data_file_input,
                            dataset_info,
                            spec_preview_output,
                            model_type_input,
                            dataset_size_input,
                            test_size_input,
                            split_info,
                            max_dataset_size_state,
                            preset_active_state,
                            preset_target_state,
                        ],
                    )
                else:
                    logger.info(f"Button {i + 1} has no example assigned")

    def _load_preset_buttons(self, *preset_buttons):
        """Load and configure preset example buttons."""
        examples = self.exp_manager.list_examples()
        logger.info(f"Retrieved examples: {[ex.get('name', ex.get('label', 'Unknown')) for ex in examples]}")

        if not examples:
            # Hide all buttons if no examples
            logger.warning("No examples returned from Master API - hiding all preset buttons")
            updates = []
            for _ in preset_buttons:
                updates.append(gr.update(visible=False))
            return tuple(updates)

        # Check if we have error examples
        error_examples = [
            ex
            for ex in examples
            if "error" in ex
            or ex.get("name")
            in ["CONNECTION_ERROR", "TIMEOUT_ERROR", "HTTP_ERROR", "REQUEST_ERROR", "UNEXPECTED_ERROR"]
        ]

        if error_examples:
            # Show error information in first button, hide others
            error_example = error_examples[0]
            error_label = error_example.get("label", "API Error")
            error_msg = error_example.get("error", "Unknown error")

            logger.error(f"API connectivity error: {error_msg}")

            updates = []
            updates.append(gr.update(value=error_label, visible=True, interactive=False))  # First button shows error
            for _ in preset_buttons[1:]:  # Hide remaining buttons
                updates.append(gr.update(visible=False))
            return tuple(updates)

        # Create updates for each button with valid examples
        updates = []
        button_names = [example.get("name", "") for example in examples if example.get("name")]
        button_labels = [example.get("label", "") for example in examples if example.get("label")]

        for i in range(len(preset_buttons)):
            if i < len(button_names):
                # Show button with human-readable label
                display_name = button_labels[i] if i < len(button_labels) else button_names[i]
                logger.info(f"Setting preset button {i + 1} to: {display_name}")
                updates.append(gr.update(value=display_name, visible=True))
            else:
                # Hide unused button
                updates.append(gr.update(visible=False))

        return tuple(updates)

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
                examples_count = result.get("examples_count", 0)
                examples = result.get("examples", [])
                examples_names = [ex.get("label", ex.get("name", "Unknown")) for ex in examples[:3]]
                examples_preview = f" (e.g., {', '.join(examples_names)})" if examples_names else ""
                return f"{message}\n📊 Found {examples_count} available examples{examples_preview}"
            else:
                suggestion = result.get("suggestion", "")
                return f"{message}\n💡 {suggestion}" if suggestion else message

        except Exception as e:
            logger.error(f"Error testing Master API connection: {e}")
            return f"❌ Failed to test API connection: {str(e)}\n💡 Try running 'make dev' to start all services"

    def _create_experiment(
        self,
        name: str,
        description: str,
        data_file,
        max_iterations: int,
        llm_model: str,
        task_type: str,
        target_field: str,
        num_classes: Optional[str],
        num_clusters: Optional[str],
        model_type: Optional[str],
        preset_example: Optional[str],
        dataset_size: Optional[float],
        test_size: float,
        enable_memory: bool = False,
        memory_namespace: str = "",
    ) -> str:
        """Create a new experiment.

        Args:
            name: Experiment name
            description: Experiment description
            data_file: Uploaded data file
            max_iterations: Maximum iterations
            llm_model: LLM model to use
            task_type: Type of task
            target_field: Target column
            num_classes: Number of classes
            num_clusters: Number of clusters
            preset_example: Preset example name

        Returns:
            Status message
        """
        # Validate experiment name
        is_valid, error = validate_experiment_name(name)
        if not is_valid:
            return f"Error: {error}"

        # Validate task type (UI-level type, without internal variants)
        is_valid, errors = validate_experiment_config(task_type, target_field, num_classes, num_clusters)
        if not is_valid:
            return f"Error: {'; '.join(errors)}"

        # Validate max iterations
        is_valid, error = validate_max_iterations(max_iterations)
        if not is_valid:
            return f"Error: {error}"

        # Validate file upload if provided
        if data_file:
            src_path = extract_source_path_from_upload(data_file)
            if src_path:
                is_valid, error = validate_file_upload(src_path, [".csv", ".json", ".txt"])
                if not is_valid:
                    return f"Error: {error}"

        try:
            # Handle data source: preset example or uploaded file
            data_path = ""
            max_dataset_size = None

            # If preset selected and no uploaded file, upload example dataset
            if preset_example and str(preset_example).strip() and (data_file is None):
                up_res = self.exp_manager.upload_example_dataset(str(preset_example).strip())
                if "error" in up_res:
                    return f"Error uploading example dataset: {up_res['error']}"
                data_path = up_res.get("data_path", "")

                # Try to get dataset size from preset
                try:
                    spec = self.exp_manager.get_example_spec(str(preset_example).strip())
                    if spec and "dataset_path" in spec:
                        ds_rel = spec.get("dataset_path", "")
                        if ds_rel:
                            base = INTERNAL_S3_API_URL.rstrip("/")
                            bucket = STORAGE_BUCKET_NAME
                            url = f"{base}/{bucket}/{ds_rel}"
                            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                                tmp_path = tmp.name
                            try:
                                resp = requests.get(url, timeout=30)
                                if resp.ok:
                                    with open(tmp_path, "wb") as f:
                                        f.write(resp.content)
                                    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                                        max_dataset_size = count_csv_rows(tmp_path)
                            finally:
                                try:
                                    if os.path.exists(tmp_path):
                                        os.unlink(tmp_path)
                                except Exception:
                                    pass
                except Exception as e:
                    logger.warning(f"Failed to count rows for preset dataset: {e}")

            # Handle uploaded file
            if data_file is not None:
                src_path = extract_source_path_from_upload(data_file)

                if src_path and os.path.exists(src_path):
                    filename = os.path.basename(src_path)

                    try:
                        max_dataset_size = count_csv_rows(src_path)
                    except Exception as e:
                        logger.warning(f"Failed to count rows in uploaded file {src_path}: {e}")

                    # Upload file directly to S3 via Master API
                    upload_result = self.exp_manager.upload_data_file(src_path, filename)

                    if "error" in upload_result:
                        return f"Error uploading file to storage: {upload_result['error']}"

                    # Use the S3 path returned by the API
                    data_path = upload_result.get("data_path", "")

                    # Delete the local temporary file since it's now in S3
                    cleanup_temp_file(src_path)
                else:
                    return "Error: Uploaded file is not accessible"

            # Validate dataset_size if provided
            if dataset_size is not None and dataset_size > 0:
                dataset_size_int = int(dataset_size)
                if max_dataset_size is not None and max_dataset_size > 0:
                    min_size = max(1, int(max_dataset_size * 0.1))
                    if dataset_size_int > max_dataset_size:
                        return f"Error: Dataset size ({dataset_size_int}) exceeds maximum available ({max_dataset_size} rows). Please use a value between {min_size} and {max_dataset_size}."
                    if dataset_size_int < min_size:
                        return f"Error: Dataset size ({dataset_size_int}) is less than minimum required ({min_size} rows, 10% of dataset). Please use a value between {min_size} and {max_dataset_size}."

            # Derive internal task type based on selected model
            effective_task_type = task_type
            if task_type == "regression" and model_type == "LightAutoML":
                effective_task_type = "regression_automl"
            elif task_type == "classification":
                if model_type == "LightAutoML":
                    effective_task_type = "classification_automl"
                elif model_type == "CatBoost":
                    effective_task_type = "classification_catboost"

            # Create experiment config
            parameters = {
                "task_type": effective_task_type,
                "task_description": description or "",
                "target_column": target_field,
                "model_type": model_type,
            }

            # Add task-specific parameters
            if task_type == "classification" and num_classes and str(num_classes).strip():
                try:
                    parameters["n_classes"] = int(str(num_classes).strip())  # type: ignore
                except ValueError:
                    pass

            if task_type == "clustering":
                try:
                    parameters["n_clusters"] = (  # type: ignore
                        int(str(num_clusters).strip()) if num_clusters and str(num_clusters).strip() else 3
                    )
                except ValueError:
                    parameters["n_clusters"] = 3  # type: ignore

            # Create experiment config
            parameters["enable_memory"] = bool(enable_memory)
            if str(memory_namespace or "").strip():
                parameters["memory_namespace"] = str(memory_namespace).strip()
            config = {
                "description": description,
                "llm_model": llm_model,
                "max_iterations": max_iterations,
                "parameters": parameters,
            }

            # Add dataset configuration if provided
            if dataset_size is not None and dataset_size > 0:
                config["dataset_size"] = int(dataset_size)
            if test_size is not None:
                config["test_size"] = float(test_size)

            # Create experiment
            result = self.exp_manager.create_experiment(name, config, data_path or "")

            if "error" in result:
                return f"Error: {result['error']}"

            return f"✅ Experiment '{name}' created successfully with ID: {result['id']}\nFile uploaded to storage: {data_path}"

        except Exception as e:
            logger.error(f"Error creating experiment: {e}")
            return f"Error creating experiment: {str(e)}"

    def _on_preset_button_click(self, example_name: str):
        """Handle preset button click.

        Args:
            example_name: Name of the preset example

        Returns:
            Tuple of UI updates
        """
        if not example_name:
            return self._get_default_preset_updates()

        data = self.exp_manager.get_example_spec(example_name)
        if not data:
            return self._get_default_preset_updates()

        spec = data.get("spec") or {}
        label = data.get("label") or example_name
        default_name = str(label)
        task = spec.get("task_type")
        desc = spec.get("task_description", "")
        target = spec.get("target_field") or None
        n_clusters = str(spec.get("n_clusters", "")) if "n_clusters" in spec else ""

        # Handle dataset download
        dataset_info_update = gr.update(value="No dataset selected")
        file_update = gr.update(value=None)
        local_file_path = None

        try:
            ds_rel = spec.get("dataset_path", "")
            # Extract filename from path (handles both relative paths and filenames)
            ds_basename = os.path.basename(ds_rel) if ds_rel else ""
            if ds_basename:
                # Update dataset info to show preset dataset
                dataset_info_update = gr.update(value=f"📁 Using preset dataset: {ds_basename}")

                # Download the file if not already present
                if not self.bucket_name:
                    self.bucket_name = self.status_service.get_storage_status()

                # Fallback to default bucket name if status service doesn't return it
                bucket_name = self.bucket_name or STORAGE_BUCKET_NAME

                if bucket_name:
                    # Use just the filename for MinIO path (files are stored as data/{filename})
                    # ds_rel might be a relative path like "../ml_evolution_templates/examples/file.csv"
                    # but in MinIO it's stored as "data/file.csv"
                    minio_path = f"data/{ds_basename}"
                    logger.debug(f"Downloading ML preset dataset: {minio_path} from bucket: {bucket_name}")
                    local_path = download_preset_dataset(minio_path, bucket_name)
                    if local_path and os.path.exists(local_path):
                        file_update = gr.update(value=local_path)
                        local_file_path = local_path
                        logger.info(f"Successfully downloaded ML preset dataset to: {local_path}")
                    else:
                        logger.warning(f"Failed to download preset dataset: {minio_path} (bucket: {bucket_name})")
                else:
                    logger.error("No bucket name available for downloading preset dataset")
        except Exception as e:
            logger.error(f"Error downloading preset dataset: {e}", exc_info=True)
            ds_rel = spec.get("dataset_path", "")
            ds_basename = os.path.basename(ds_rel) if ds_rel else "Preset dataset"
            if ds_basename:
                dataset_info_update = gr.update(value=f"📁 Using preset dataset: {ds_basename}")

        # Generate spec preview
        spec_preview = self.build_spec_preview(
            description=desc,
            task_type=task or "",
            target_field=target or "",
            num_classes="",
            num_clusters=n_clusters,
            data_file_path=local_file_path,
            preset_example=example_name,
        )
        spec_preview_update = gr.update(value=spec_preview)

        # Count rows in dataset if file is available
        dataset_size_update = gr.update(value=None)
        max_size_value = None
        if local_file_path and os.path.exists(local_file_path):
            try:
                file_size = os.path.getsize(local_file_path)
                logger.debug(f"Counting rows in ML preset dataset file: {local_file_path} (size: {file_size} bytes)")
                row_count = count_csv_rows(local_file_path)
                if row_count is not None and row_count > 0:
                    logger.info(f"Counted {row_count} rows in ML preset dataset: {local_file_path}")
                    dataset_size_update = gr.update(value=row_count)
                    max_size_value = row_count
                else:
                    logger.warning(
                        f"Failed to count rows or empty dataset: {local_file_path} (file exists: {os.path.exists(local_file_path)}, size: {file_size})"
                    )
            except Exception as e:
                logger.error(f"Error counting rows in dataset {local_file_path}: {e}", exc_info=True)
        else:
            if local_file_path:
                logger.warning(f"ML preset dataset file does not exist: {local_file_path}")
            else:
                logger.debug("No local file path available for ML preset row counting")

        # Update UI based on task type
        if task == "classification":
            # Default model for classification preset
            model_update = gr.update(
                choices=["LogisticRegression", "LightAutoML", "CatBoost"],
                value="LogisticRegression",
                visible=True,
            )
            return (
                gr.update(value=desc),  # description_input
                gr.update(value="classification"),  # task_type_input
                gr.update(choices=[target] if target else [], value=target, visible=True),  # target_field_input
                gr.update(visible=True, value=""),  # num_classes_input
                gr.update(visible=False, value=""),  # num_clusters_input
                gr.update(visible=False),  # clusters_hint
                gr.update(value=default_name),  # name_input
                file_update,  # data_file_input
                dataset_info_update,  # dataset_info
                spec_preview_update,  # spec_preview_output
                model_update,  # model_type_input
                dataset_size_update,  # dataset_size_input
                gr.update(value=0.2),  # test_size_input
                gr.update(value="", visible=False),  # split_info
                max_size_value,  # max_dataset_size_state
                example_name,  # preset_active_state
                target,  # preset_target_state
            )
        elif task == "regression":
            # Default model for regression preset
            model_update = gr.update(
                choices=["Ridge", "LightAutoML"],
                value="Ridge",
                visible=True,
            )
            return (
                gr.update(value=desc),  # description_input
                gr.update(value="regression"),  # task_type_input
                gr.update(choices=[target] if target else [], value=target, visible=True),  # target_field_input
                gr.update(visible=False, value=""),  # num_classes_input
                gr.update(visible=False, value=""),  # num_clusters_input
                gr.update(visible=False),  # clusters_hint
                gr.update(value=default_name),  # name_input
                file_update,  # data_file_input
                dataset_info_update,  # dataset_info
                spec_preview_update,  # spec_preview_output
                model_update,  # model_type_input
                dataset_size_update,  # dataset_size_input
                gr.update(value=0.2),  # test_size_input
                gr.update(value="", visible=False),  # split_info
                max_size_value,  # max_dataset_size_state
                example_name,  # preset_active_state
                target,  # preset_target_state
            )
        elif task == "clustering":
            # No model selection for clustering
            model_update = gr.update(choices=[], value=None, visible=False)
            return (
                gr.update(value=desc),  # description_input
                gr.update(value="clustering"),  # task_type_input
                gr.update(choices=[], value=None, visible=False),  # target_field_input
                gr.update(visible=False, value=""),  # num_classes_input
                gr.update(visible=True, value=n_clusters),  # num_clusters_input
                gr.update(visible=True),  # clusters_hint
                gr.update(value=default_name),  # name_input
                file_update,  # data_file_input
                dataset_info_update,  # dataset_info
                spec_preview_update,  # spec_preview_output
                model_update,  # model_type_input
                dataset_size_update,  # dataset_size_input
                gr.update(value=0.2),  # test_size_input
                gr.update(value="", visible=False),  # split_info
                max_size_value,  # max_dataset_size_state
                example_name,  # preset_active_state
                target,  # preset_target_state
            )

        # Default case
        return self._get_default_preset_updates()

    def _get_default_preset_updates(self):
        """Get default preset updates for when no example is selected."""
        return (
            gr.update(value=""),
            gr.update(value=None),
            gr.update(choices=[], value=None, visible=False),
            gr.update(visible=False, value=""),
            gr.update(visible=False, value=""),
            gr.update(visible=False),
            gr.update(value=""),
            gr.update(value=None),
            gr.update(value="No dataset selected"),
            gr.update(value=""),
            gr.update(choices=[], value=None, visible=False),
            gr.update(value=None),  # dataset_size_input
            gr.update(value=0.2),  # test_size_input
            gr.update(value="", visible=False),  # split_info
            None,  # max_dataset_size_state
            None,
            None,
        )
