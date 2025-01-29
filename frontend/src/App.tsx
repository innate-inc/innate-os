import "./App.css";

function App() {
  return (
    <div className="App">
      <h1>My Simulation Viewer</h1>
      <img
        src="http://localhost:8000/video_feed"
        alt="First Person"
        style={{ width: 640, border: "1px solid #ccc" }}
      />
      <img
        src="http://localhost:8000/video_feed_chase"
        alt="Chase Camera"
        style={{ width: 640, border: "1px solid #ccc" }}
      />
    </div>
  );
}

export default App;
