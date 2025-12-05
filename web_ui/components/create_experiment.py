"""Create Experiment tab component."""

import os
from typing import Optional

import gradio as gr
from config.settings import (
    AVAILABLE_LLM_MODELS,
    TASK_TYPES,
    VALIDATION_RULES,
)
from loguru import logger
from utils.file_handlers import (
    cleanup_temp_file,
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
                        choices=AVAILABLE_LLM_MODELS,
                        value=AVAILABLE_LLM_MODELS[0],
                        label="LLM Model",
                    )
                    spec_preview_btn = gr.Button("Preview Spec JSON")
                    spec_preview_output = gr.Code(label="Spec JSON", language="json")

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
                llm_model_input,
                preset_btn_1,
                preset_btn_2,
                preset_btn_3,
                preset_btn_4,
                preset_btn_5,
                preset_btn_6,
                preset_btn_7,
                preset_btn_8,
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
            llm_model_input,
            *preset_buttons_debug,
        ) = inputs

        # Separate preset buttons from debug components
        preset_buttons = preset_buttons_debug[:-3]

        # Update target choices when file changes
        def _update_target_choices(file, task_type, preset_active, preset_target):
            src_path = extract_source_path_from_upload(file)
            file_columns = read_csv_columns(src_path) if src_path else []

            # Update dataset info when user uploads a file
            dataset_info_update = gr.update()
            if src_path:
                filename = os.path.basename(src_path)
                dataset_info_update = gr.update(value=f"📁 Using uploaded file: {filename}")

            visible, choices, value = update_dropdown_choices(src_path, task_type, preset_target, file_columns)
            return gr.update(choices=choices, value=value, visible=visible), dataset_info_update

        data_file_input.change(
            _update_target_choices,
            inputs=[data_file_input, task_type_input, preset_active_state, preset_target_state],
            outputs=[target_field_input, dataset_info],
        )

        # Show/hide inputs depending on task type
        def _on_task_type_change(task_type, file, preset_active, preset_target):
            src_path = extract_source_path_from_upload(file)
            file_columns = read_csv_columns(src_path) if src_path else []

            # Handle preset or file-based target field
            if preset_active:
                visible, choices, value = update_dropdown_choices(src_path, task_type, preset_target, [])
                if task_type == "classification":
                    return (
                        gr.update(choices=choices, visible=True, value=value),
                        gr.update(visible=True, value=""),
                        gr.update(visible=False, value=""),
                        gr.update(visible=False),
                    )
                elif task_type == "regression":
                    return (
                        gr.update(choices=choices, visible=True, value=value),
                        gr.update(visible=False, value=""),
                        gr.update(visible=False, value=""),
                        gr.update(visible=False),
                    )
                elif task_type == "clustering":
                    return (
                        gr.update(visible=False, value=None),
                        gr.update(visible=False, value=""),
                        gr.update(visible=True, value="3"),
                        gr.update(visible=True),
                    )
            else:
                choices = get_default_target_choices(task_type, file_columns)
                if task_type == "classification":
                    return (
                        gr.update(choices=choices, visible=True, value=choices[0] if choices else None),
                        gr.update(visible=True, value=""),
                        gr.update(visible=False, value=""),
                        gr.update(visible=False),
                    )
                elif task_type == "regression":
                    return (
                        gr.update(choices=choices, visible=True, value=choices[0] if choices else None),
                        gr.update(visible=False, value=""),
                        gr.update(visible=False, value=""),
                        gr.update(visible=False),
                    )
                elif task_type == "clustering":
                    return (
                        gr.update(visible=False, value=None),
                        gr.update(visible=False, value=""),
                        gr.update(visible=True, value="3"),
                        gr.update(visible=True),
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
            outputs=[target_field_input, num_classes_input, num_clusters_input, clusters_hint],
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
                preset_active_state,
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
        preset_example: Optional[str],
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

        # Validate task type
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

            # If preset selected and no uploaded file, upload example dataset
            if preset_example and str(preset_example).strip() and (data_file is None):
                up_res = self.exp_manager.upload_example_dataset(str(preset_example).strip())
                if "error" in up_res:
                    return f"Error uploading example dataset: {up_res['error']}"
                data_path = up_res.get("data_path", "")

            # Handle uploaded file
            if data_file is not None:
                src_path = extract_source_path_from_upload(data_file)

                if src_path and os.path.exists(src_path):
                    filename = os.path.basename(src_path)

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

            # Create experiment config
            parameters = {
                "task_type": task_type,
                "task_description": description or "",
                "target_column": target_field,
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
            config = {
                "description": description,
                "llm_model": llm_model,
                "max_iterations": max_iterations,
                "timeout_seconds": 3600,
                "parameters": parameters,
            }

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
            ds_basename = os.path.basename(ds_rel) if ds_rel else ""
            if ds_basename:
                # Update dataset info to show preset dataset
                dataset_info_update = gr.update(value=f"📁 Using preset dataset: {ds_basename}")

                # Download the file if not already present
                if not self.bucket_name:
                    self.bucket_name = self.status_service.get_storage_status()

                if self.bucket_name:
                    local_path = download_preset_dataset(ds_rel, self.bucket_name)
                    if local_path:
                        file_update = gr.update(value=local_path)
                        local_file_path = local_path
        except Exception:
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

        # Update UI based on task type
        if task == "classification":
            return (
                gr.update(value=desc),
                gr.update(value="classification"),
                gr.update(choices=[target] if target else [], value=target, visible=True),
                gr.update(visible=True, value=""),
                gr.update(visible=False, value=""),
                gr.update(visible=False),
                gr.update(value=default_name),
                file_update,
                dataset_info_update,
                spec_preview_update,
                example_name,
                target,
            )
        elif task == "regression":
            return (
                gr.update(value=desc),
                gr.update(value="regression"),
                gr.update(choices=[target] if target else [], value=target, visible=True),
                gr.update(visible=False, value=""),
                gr.update(visible=False, value=""),
                gr.update(visible=False),
                gr.update(value=default_name),
                file_update,
                dataset_info_update,
                spec_preview_update,
                example_name,
                target,
            )
        elif task == "clustering":
            return (
                gr.update(value=desc),
                gr.update(value="clustering"),
                gr.update(choices=[], value=None, visible=False),
                gr.update(visible=False, value=""),
                gr.update(visible=True, value=n_clusters),
                gr.update(visible=True),
                gr.update(value=default_name),
                file_update,
                dataset_info_update,
                spec_preview_update,
                example_name,
                target,
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
            None,
            None,
        )
