#!/usr/bin/env python3

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class ExperimentStatus(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    PREPARED = "prepared"
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    INITIALIZING = "initializing"
    DEPLOYED = "deployed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TERMINATED = "terminated"
    PREPARATION_FAILED = "preparation_failed"
    CANCELLED = "cancelled"


class ExperimentConfig(BaseModel):
    description: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    llm_model: str = "local-inference"
    prompt_llm_model: Optional[str] = Field(
        None, description="Optional separate model id for PROMPT_* variables (prompt templates)"
    )
    max_iterations: int = 100
    timeout_seconds: Optional[int] = Field(None, description="Deprecated – experiments stop by max_iterations only")
    dataset_size: Optional[int] = Field(None, description="Maximum number of rows to use from dataset (None = use all)")
    test_size: Optional[float] = Field(0.2, description="Fraction of dataset to use for testing (0.0-1.0)")


class PromptValidationCriteria(BaseModel):
    """Validation criteria for prompt experiments."""

    validation_type: Literal["Binary (0/1)", "Continuous (0..1)"] = Field(
        ..., description="Binary (0/1) or Continuous (0..1)"
    )
    binary_method: Optional[Literal["equality", "substring", "regexp"]] = Field(
        None, description="Method for binary validation: equality, substring, or regexp"
    )
    regexp_pattern: Optional[str] = Field(
        None, description="Regular expression pattern to extract answer from LLM output"
    )
    continuous_metric: Optional[Literal["ROUGE-1", "ROUGE-2", "ROUGE-L", "BERTScore", "BLEU"]] = Field(
        None,
        description="Metric for continuous validation: ROUGE-1, ROUGE-2, ROUGE-L, BERTScore, BLEU",
    )


class ChainValidationCriteria(BaseModel):
    validation_type: Literal["Binary (0/1)", "Continuous (0..1)"] = Field(
        "Binary (0/1)", description="Binary (0/1) or Continuous (0..1)"
    )
    binary_method: Optional[Literal["equality", "substring", "regexp"]] = Field(
        "equality", description="Method for binary validation: equality, substring, or regexp"
    )
    regexp_pattern: Optional[str] = Field(
        None, description="Regular expression pattern to extract answer from chain output"
    )
    continuous_metric: Optional[Literal["ROUGE-1", "ROUGE-2", "ROUGE-L", "BERTScore", "BLEU"]] = Field(
        None,
        description="Metric for continuous validation: ROUGE-1, ROUGE-2, ROUGE-L, BERTScore, BLEU",
    )


class PromptExperimentCreate(BaseModel):
    """Input schema for creating prompt-based experiments."""

    name: str = Field(..., description="Experiment name")
    description: Optional[str] = Field(None, description="Optional experiment description")
    data_path: str = Field(..., description="Path to the uploaded data file")
    target_column: str = Field(..., description="Target column to predict")
    base_prompt: str = Field(..., description="Base prompt template with {column} placeholders")

    validation_criteria: PromptValidationCriteria = Field(..., description="Validation criteria configuration")

    llm_model: str = Field("local-inference", description="LLM model to use for prompt evolution")
    prompt_llm_model: Optional[str] = Field(
        None, description="Optional separate model id for PROMPT_* variables (prompt templates)"
    )
    max_iterations: int = Field(100, ge=1, le=1000, description="Maximum number of evolution iterations")
    dataset_size: Optional[int] = Field(
        None, ge=1, description="Maximum number of rows to use from dataset (None = use all)"
    )
    test_size: Optional[float] = Field(
        0.2, ge=0.0, le=1.0, description="Fraction of dataset to use for testing (0.0-1.0)"
    )
    enable_memory: Optional[bool] = Field(False, description="Enable memory card retrieval during evolution mutations")
    memory_namespace: Optional[str] = Field(
        None, description="Optional memory namespace to share retrieval/write-back across experiments"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Customer Sentiment Analysis",
                "description": "Analyze customer reviews to predict sentiment",
                "data_path": "data/customer_reviews.csv",
                "target_column": "sentiment",
                "base_prompt": "You are a sentiment analyst. Review: {review_text} Rating: {rating} Category: {product_category}\nAnswer:",
                "validation_criteria": {
                    "validation_type": "Binary (0/1)",
                    "binary_method": "equality",
                    "regexp_pattern": "Answer:\\s*(.+?)$",
                },
                "llm_model": "local-inference",
                "max_iterations": 100,
            }
        }


class ExperimentCreate(BaseModel):
    name: str
    config: ExperimentConfig
    data_path: str


class Experiment(BaseModel):
    id: str = Field(default_factory=lambda: f"exp_{uuid4()}")
    name: str
    status: ExperimentStatus = ExperimentStatus.PENDING
    config: ExperimentConfig
    data_path: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)
    best_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    status_message: Optional[str] = None


class ChainExperimentCreate(BaseModel):
    name: str = Field(..., description="Experiment name")
    description: Optional[str] = Field(None, description="Optional experiment description")
    data_path: str = Field(..., description="Path to the uploaded data file")
    target_column: str = Field(..., description="Target column to predict")
    base_chain_config: str = Field(..., description="Base chain configuration JSON")
    frozen_steps: Optional[list[int]] = Field(
        default=None, description="List of step numbers to freeze (do not evolve)"
    )
    chain_size_limit: Optional[int] = Field(
        default=None, ge=1, le=1000, description="Maximum allowed number of steps in the chain"
    )

    validation_criteria: ChainValidationCriteria = Field(
        default_factory=ChainValidationCriteria,
        description="Validation criteria configuration for chain outputs",
    )

    llm_model: str = Field("local-inference", description="LLM model to use for chain evolution")
    max_iterations: int = Field(100, ge=1, le=1000, description="Maximum number of evolution iterations")
    dataset_size: Optional[int] = Field(
        None, ge=1, description="Maximum number of rows to use from dataset (None = use all)"
    )
    test_size: Optional[float] = Field(
        0.2, ge=0.0, le=1.0, description="Fraction of dataset to use for testing (0.0-1.0)"
    )
    evolution_mode: Optional[str] = Field("full_chain", description="Evolution mode: 'full_chain' or 'single_step'")
    step_number: Optional[int] = Field(
        None, ge=1, description="Step number to evolve (1-based, only for single_step mode)"
    )
    enable_memory: Optional[bool] = Field(False, description="Enable memory card retrieval during evolution mutations")
    memory_namespace: Optional[str] = Field(
        None, description="Optional memory namespace to share retrieval/write-back across experiments"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "name": "GSM8K Chain Evolution",
                "description": "Evolve reasoning chains for GSM8K math problems",
                "data_path": "data/gsm8k/train.csv",
                "target_column": "target",
                "base_chain_config": '{"steps": [{"number": 1, "title": "Problem Understanding", ...}]}',
                "frozen_steps": [1],
                "chain_size_limit": 8,
                "llm_model": "local-inference",
                "max_iterations": 100,
            }
        }


class CARLChainExperimentCreate(BaseModel):
    """Input schema for creating CARL-typed chain experiments."""

    name: str = Field(..., description="Experiment name")
    description: Optional[str] = Field(None, description="Optional experiment description")
    # For CARL we still use uploaded dataset path (already uploaded via web)
    data_path: Optional[str] = Field(None, description="Path to the uploaded data file (optional)")
    target_column: str = Field(..., description="Target column to predict")
    base_chain_config: str = Field(..., description="Typed CARL chain configuration JSON")
    frozen_steps: Optional[list[int]] = Field(
        default=None, description="List of step numbers to freeze (do not evolve)"
    )
    chain_size_limit: Optional[int] = Field(
        default=None, ge=1, le=1000, description="Maximum allowed number of steps in the chain"
    )
    evolution_mode: Optional[str] = Field(
        "full_chain",
        description="Evolution mode: 'full_chain' evolves all non-frozen steps, "
        "'single_step' evolves only the specified step_number",
    )
    step_number: Optional[int] = Field(
        None, ge=1, description="Step number to evolve (required when evolution_mode='single_step')"
    )
    llm_model: str = Field("local-inference", description="LLM model to use for evolution")
    chain_llm_model: Optional[str] = Field(
        None,
        description="LLM model for chain execution (if different from evolution model). "
        "When set, the CARL chain steps will use this model instead of llm_model.",
    )
    max_iterations: int = Field(100, ge=1, le=1000, description="Maximum number of evolution iterations")
    dataset_size: Optional[int] = Field(
        None, ge=1, description="Maximum number of rows to use from dataset (None = use all)"
    )
    test_size: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Fraction of dataset to use for testing (0.0-1.0)"
    )
    python_code: Optional[str] = Field(None, description="Custom Python code for TOOL steps (saved as custom_tools.py)")
    enable_feedback: Optional[bool] = Field(
        False, description="Enable chain execution feedback generation for additional reflection"
    )
    feedback_template: Optional[str] = Field(
        "detailed", description="Feedback template style: 'detailed', 'summary', or 'errors_only'"
    )
    validation_criteria: Optional[ChainValidationCriteria] = Field(
        default_factory=ChainValidationCriteria,
        description="Validation criteria for fitness evaluation",
    )
    enable_memory: Optional[bool] = Field(False, description="Enable memory card retrieval during evolution mutations")
    memory_namespace: Optional[str] = Field(None, description="Memory namespace (remote memory bank name for API mode)")


class ExperimentUpdate(BaseModel):
    status: Optional[ExperimentStatus] = None
    metrics: Optional[Dict[str, Any]] = None
    best_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    status_message: Optional[str] = None
