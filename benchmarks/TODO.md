# Benchmark Framework Implementation Roadmap

This document outlines the remaining implementation tasks for the agent benchmarking framework.

## High Priority Tasks

1. **Check Validation Implementation**
   - [ ] Implement location check validation using robot position data
   - [ ] Implement primitive call validation using API logs
   - [ ] Implement compound check validation (action in location)
   - [ ] Implement sequence check validation
   - [ ] Implement VLM-based verification for behavior checks

2. **Environment Setup Implementation**
   - [ ] Implement API calls to set robot position and orientation
   - [ ] Implement API calls to place objects at specified positions
   - [ ] Add verification of environment setup

3. **Early Stopping Implementation**
   - [x] Implement framework for VLM-based verification of stop criterion
   - [ ] Add VLM API key configuration
   - [x] Add frame selection logic for VLM analysis
   - [ ] Complete VLM response analysis implementation

## Medium Priority Tasks

1. **Periodic Check Validation**
   - [ ] Implement periodic validation of all checks during benchmark run
   - [ ] Add check result logging to metrics

2. **Results Analysis Enhancements**
   - [ ] Update analyze_results.py to handle check results
   - [ ] Add check success rate visualization
   - [ ] Create timeline visualization of check completions

3. **VLM Integration**
   - [x] Implement framework for VLM analysis
   - [x] Add placeholder for VLM API integration
   - [ ] **IMPORTANT**: Add your VLM API key in the `_evaluate_with_vlm` method
   - [ ] Implement image encoding for VLM API
   - [ ] Complete response parsing and analysis

## Low Priority Tasks

1. **Benchmark Category Completion**
   - [ ] Create remaining task configurations for all categories
   - [ ] Add specific task validation for specialized tests

2. **UI Enhancements**
   - [ ] Add real-time status display during benchmark runs
   - [ ] Implement progress visualization

3. **Performance Optimizations**
   - [ ] Optimize frame capture and storage
   - [ ] Implement selective check validation for performance

## Architecture Decisions

1. **VLM Integration Strategy**
   - [x] Decided to use GPT-4o with structured JSON output
   - [x] Defined standard prompting format for verification
   - [x] Established frame selection criteria (alternating cameras at intervals)

2. **Check Validation Frequency**
   - Decide how often to run check validations
   - Balance between validation thoroughness and performance

3. **Early Stopping Criteria**
   - [x] Implemented framework for early stopping based on VLM analysis
   - [x] Added structured output format for stop decisions
   - [ ] Ensure consistency in early stopping across different tasks

## Benchmark Methodology

1. **Success Criteria Standardization**
   - [x] Implemented framework for consistent success criteria evaluation
   - [ ] Ensure objective measurement where possible

2. **Task Completion Time**
   - [x] Added tracking of time_since_start in chat messages
   - [ ] Implement time-to-completion tracking for specific checks

3. **Failure Analysis**
   - [x] Added structured output for failure reasons in VLM responses
   - [ ] Implement automatic failure categorization where possible

## VLM API Key Setup

To fully enable VLM-based verification, you need to:

1. Get an API key for GPT-4o or another capable VLM
2. Add the API key to the `_evaluate_with_vlm` method in benchmark_runner.py
3. Uncomment the actual API call code and remove the mock response

```python
# In benchmark_runner.py:
def _evaluate_with_vlm(self, criterion, frame_paths, is_stop_check=False):
    # Add your API key here
    vlm_api_key = "your_api_key_here"  # or use an environment variable
    
    # The rest of the implementation will use this key
``` 