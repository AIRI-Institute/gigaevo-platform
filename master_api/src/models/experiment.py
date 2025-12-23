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
    prompt_llm_model: Optional[str] = Field(
        None, description="Optional separate model id for PROMPT_* variables (prompt templates)"
    )
    max_iterations: int = 100
    timeout_seconds: int = 3600


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


class PromptExperimentCreate(BaseModel):
    """Input schema for creating prompt-based experiments."""

    name: str = Field(..., description="Experiment name")
    description: Optional[str] = Field(None, description="Optional experiment description")
    data_path: str = Field(..., description="Path to the uploaded data file")
    target_column: str = Field(..., description="Target column to predict")
    base_prompt: str = Field(..., description="Base prompt template with {column} placeholders")

    validation_criteria: PromptValidationCriteria = Field(
        ..., description="Validation criteria configuration"
    )

    llm_model: str = Field("local-inference", description="LLM model to use for prompt evolution")
    prompt_llm_model: Optional[str] = Field(
        None, description="Optional separate model id for PROMPT_* variables (prompt templates)"
    )
    max_iterations: int = Field(100, ge=1, le=1000, description="Maximum number of evolution iterations")

    class Config:
        schema_extra = {
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


class ExperimentUpdate(BaseModel):
    status: Optional[ExperimentStatus] = None
    metrics: Optional[Dict[str, Any]] = None
    best_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
