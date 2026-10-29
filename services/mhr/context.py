"""Bundled Rise authoring resources; callers can supply their own registries."""
from pathlib import Path

MHR_REGISTRY = Path(__file__).resolve().parents[2] / 'resources/data/dumps/rszmhrise.json'
