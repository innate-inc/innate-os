# Benchmark Results Viewer

A React-based viewer for visualizing benchmark results.

## Setup & Running

```bash
# Install dependencies
npm install

# Start the development server
npm run dev
```

## Important: Syncing Results

The viewer reads benchmark results from its `public/results` directory. To view the latest benchmark results, you must sync them from the main results directory using the provided sync script:

```bash
# From the benchmark-viewer directory
./sync_results.py
```

### What the Sync Script Does

1. Copies all benchmark result directories from `../results/` to `public/results/`
2. Generates an `index.json` file listing all available benchmarks
3. Removes any old results that no longer exist in the source directory

### When to Run the Sync Script

You need to run the sync script:
- After running new benchmarks
- After deleting old benchmark results
- Any time you want to refresh the data shown in the viewer

The viewer does not automatically detect changes in the results directory - you must manually run the sync script and refresh the browser page to see updated results.

## Development

The viewer is built with:
- React + TypeScript
- Vite
- Styled Components

Key files:
- `src/App.tsx` - Main application component
- `src/components/` - React components for different parts of the UI
- `sync_results.py` - Script for syncing benchmark results
