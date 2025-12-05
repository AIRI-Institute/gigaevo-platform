#!/usr/bin/env python3

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class ExperimentStatus(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    PREPARED = "prepared"
    INITIALIZING = "initializing"
    DEPLOYED = "deployed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExperimentConfig(BaseModel):
    description: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    llm_model: str = "local-inference"
    max_iterations: int = 100
    timeout_seconds: int = 3600


class PromptValidationCriteria(BaseModel):
    """Validation criteria for prompt experiments."""

    validation_type: str = Field(..., description="Binary (0/1) or Continuous (0..1)")
    binary_method: Optional[str] = Field(
        None, description="Method for binary validation: equality, occurrence of a substring, RegExp"
    )
    regexp_pattern: Optional[str] = Field(None, description="Regular expression pattern for substring matching")
    continuous_metric: Optional[str] = Field(
        None,
        description="Metric for continuous validation: ROUGE-1, ROUGE-2, ROUGE-L, ROUGE-Lsum, METEOR, BERTScore, AlignScore",
    )


class PromptExperimentCreate(BaseModel):
    """Input schema for creating prompt-based experiments."""

    name: str = Field(..., description="Experiment name")
    description: Optional[str] = Field(None, description="Optional experiment description")
    data_path: str = Field(..., description="Path to the uploaded data file")
    target_column: str = Field(..., description="Target column to predict")
    base_prompt: str = Field(..., description="Base prompt template with {column} placeholders")

    # TEMPORARY: Support both simplified task_type and complex validation_criteria
    # FUTURE: Make validation_criteria required and remove task_type when needed
    task_type: Optional[str] = Field(
        None, description="TEMPORARY: Task type (classification, multi_choice, math, summarization)"
    )
    validation_criteria: Optional[PromptValidationCriteria] = Field(
        None, description="FUTURE: Validation criteria configuration"
    )

    llm_model: str = Field("local-inference", description="LLM model to use for prompt evolution")
    max_iterations: int = Field(100, ge=1, le=1000, description="Maximum number of evolution iterations")

    class Config:
        schema_extra = {
            "example": {
                "name": "Customer Sentiment Analysis",
                "description": "Analyze customer reviews to predict sentiment",
                "data_path": "data/customer_reviews.csv",
                "target_column": "sentiment",
                "base_prompt": "You are a sentiment analyst. Review: {review_text} Rating: {rating} Category: {product_category} Sentiment:",
                # TEMPORARY: Simplified task_type example
                "task_type": "classification",
                # FUTURE: complex validation criteria example
                # "validation_criteria": {
                #     "validation_type": "Binary (0/1)",
                #     "binary_method": "equality",
                #     "regexp_pattern": None,
                #     "continuous_metric": None
                # },
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


class ExperimentUpdate(BaseModel):
    status: Optional[ExperimentStatus] = None
    metrics: Optional[Dict[str, Any]] = None
    best_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
