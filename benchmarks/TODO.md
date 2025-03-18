# Benchmark Framework Implementation Roadmap

This document outlines the remaining implementation tasks for the agent benchmarking framework.

## High Priority Tasks

1. **Check Validation Implementation**
   - [ ] Implement location check validation using robot position data
   - [ ] Implement primitive call validation using API logs
   - [ ] Implement compound check validation (action in location)
   - [ ] Implement sequence check validation
   - [x] Implement VLM-based verification for behavior checks

2. **Environment Setup Implementation**
   - [ ] Implement API calls to set robot position and orientation
   - [ ] Implement API calls to place objects at specified positions
   - [ ] Add verification of environment setup

3. **Early Stopping Implementation**
   - [x] Implement framework for VLM-based verification of stop criterion
   - [x] Add VLM API key configuration
   - [x] Add frame selection logic for VLM analysis
   - [x] Complete VLM response analysis implementation

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
   - [x] Add VLM API key in the `_evaluate_with_vlm` method
   - [x] Implement image encoding for VLM API
   - [x] Complete response parsing and analysis

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
   - [x] Added proper error handling for API calls

2. **Check Validation Frequency**
   - Decide how often to run check validations
   - Balance between validation thoroughness and performance

3. **Early Stopping Criteria**
   - [x] Implemented framework for early stopping based on VLM analysis
   - [x] Added structured output format for stop decisions
   - [x] Ensure consistency in early stopping across different tasks

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

The VLM API key for GPT-4o has been set up in the benchmarks/.env file. The benchmark runner will automatically load this key and use it for VLM analysis.

To use a different API key:

1. Edit the benchmarks/.env file and replace the existing key
2. Make sure your API key has access to the gpt-4o-2024-08-06 model

The benchmark runner is now fully configured to use structured JSON output with the OpenAI API.

```python
# In benchmark_runner.py:
def _evaluate_with_vlm(self, criterion, frame_paths, is_stop_check=False):
    # Add your API key here
    vlm_api_key = "your_api_key_here"  # or use an environment variable
    
    # The rest of the implementation will use this key
``` 