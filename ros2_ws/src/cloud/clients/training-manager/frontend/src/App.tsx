import { useState } from "react";
import Layout from "./components/Layout";
import SkillsTab from "./components/skills/SkillsTab";
import DatasetsTab from "./components/datasets/DatasetsTab";
import TrainingTab from "./components/training/TrainingTab";

type Tab = "skills" | "datasets" | "training";

export default function App() {
  const [tab, setTab] = useState<Tab>("skills");

  return (
    <Layout activeTab={tab} onTabChange={setTab}>
      {tab === "skills" && <SkillsTab />}
      {tab === "datasets" && <DatasetsTab />}
      {tab === "training" && <TrainingTab />}
    </Layout>
  );
}
