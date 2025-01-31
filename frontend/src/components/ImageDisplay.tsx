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
  const useSSL = import.meta.env.VITE_USE_SSL ?? false;

  let mainSrc = `http${useSSL ? "s" : ""}://${ip}:8000/video_feed`;
  let subSrc = `http${useSSL ? "s" : ""}://${ip}:8000/video_feed_chase`;

  if (viewMode === "chaseFocus") {
    mainSrc = `http${useSSL ? "s" : ""}://${ip}:8000/video_feed_chase`;
    subSrc = `http${useSSL ? "s" : ""}://${ip}:8000/video_feed`;
  } else if (viewMode === "frontFocus") {
    mainSrc = `http${useSSL ? "s" : ""}://${ip}:8000/video_feed`;
    subSrc = `http${useSSL ? "s" : ""}://${ip}:8000/video_feed_chase`;
  }

  return (
    <PreviewContainer>
      <MainImage viewMode={viewMode} src={mainSrc} alt="Main Camera" />
      <SecondaryImage viewMode={viewMode} src={subSrc} alt="Sub Camera" />
    </PreviewContainer>
  );
}
