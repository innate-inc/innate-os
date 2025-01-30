import {
  PreviewContainer,
  MainImage,
  SecondaryImage,
} from "../styles/StyledImages";

type ImageDisplayProps = {
  viewMode: "sideBySide" | "frontFocus" | "chaseFocus";
};

export function ImageDisplay({ viewMode }: ImageDisplayProps) {
  // Grab IP from environment, use a fallback if missing
  const ip = import.meta.env.VITE_SIM_IP ?? "localhost";

  let mainSrc = `http://${ip}:8000/video_feed`;
  let subSrc = `http://${ip}:8000/video_feed_chase`;

  if (viewMode === "chaseFocus") {
    mainSrc = `http://${ip}:8000/video_feed_chase`;
    subSrc = `http://${ip}:8000/video_feed`;
  } else if (viewMode === "frontFocus") {
    mainSrc = `http://${ip}:8000/video_feed`;
    subSrc = `http://${ip}:8000/video_feed_chase`;
  }

  return (
    <PreviewContainer>
      <MainImage viewMode={viewMode} src={mainSrc} alt="Main Camera" />
      <SecondaryImage viewMode={viewMode} src={subSrc} alt="Sub Camera" />
    </PreviewContainer>
  );
}
