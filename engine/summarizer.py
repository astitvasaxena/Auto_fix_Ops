from typing import Dict, Any, List
from pydantic import BaseModel, Field

class IncidentSummary(BaseModel):
    cpu_peak: float = Field(description="Peak CPU usage in the window, in CPU cores (0.5 = 500m).")
    cpu_trend: str = Field(description="One of: HIGH_SUSTAINED, CLIMBING, STABLE, UNKNOWN")
    memory_peak: float = Field(description="Peak memory usage in the window, in bytes.")
    memory_trend: str = Field(description="One of: RAPID_CLIMB, SLOW_LEAK, STABLE, UNKNOWN")
    context_tags: List[str] = Field(description="Metadata and inferred symptom labels.")

class ContextSummarizer:
    """
    Transforms raw Prometheus traces into constrained deterministic context.
    Prevents the LLM from hallucinating over noisy float arrays.
    """
    
    @staticmethod
    def _calculate_trend(data_points: List[float], threshold: float) -> str:
        if not data_points: return "UNKNOWN"
        if len(data_points) < 2: return "STABLE"
        
        start, end = data_points[0], data_points[-1]
        peak = max(data_points)
        
        if peak > threshold and (sum(data_points) / len(data_points)) > (threshold * 0.8):
            return "HIGH_SUSTAINED"
        if end - start > 0.3:
            return "RAPID_CLIMB" if end - start > 0.6 else "SLOW_LEAK"
            
        return "STABLE"

    @classmethod
    def summarize(cls, mongo_doc: Dict[str, Any]) -> IncidentSummary:
        metrics = mongo_doc.get("metrics", {})
        
        # Extract lists of values avoiding string timestamp keys if complex
        # Assuming fetch_prometheus_metric stored standard promql list
        cpu_raw = metrics.get("cpu", [])
        mem_raw = metrics.get("memory", [])
        
        # Safely extract the float scalar [1] from standard Prometheus raw result: [timestamp, "value"]
        cpu_floats = [float(point["value"][1]) for point in cpu_raw] if cpu_raw and isinstance(cpu_raw[0], dict) else [0.0]
        mem_floats = [float(point["value"][1]) for point in mem_raw] if mem_raw and isinstance(mem_raw[0], dict) else [0.0]
        
        cpu_peak = max(cpu_floats) if cpu_floats else 0.0
        mem_peak = max(mem_floats) if mem_floats else 0.0
        
        return IncidentSummary(
            cpu_peak=round(cpu_peak, 2),
            cpu_trend=cls._calculate_trend(cpu_floats, 0.8),
            memory_peak=round(mem_peak, 2),
            memory_trend=cls._calculate_trend(mem_floats, 0.8),
            context_tags=["resource_exhaustion"] if mem_peak > 0.9 or cpu_peak > 0.9 else []
        )
