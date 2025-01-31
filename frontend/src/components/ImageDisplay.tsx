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
  const baseUrl = import.meta.env.VITE_BASE_URL ?? "http://localhost:8000";

  let mainSrc = baseUrl + "/video_feed";
  let subSrc = baseUrl + "/video_feed_chase";

  if (viewMode === "chaseFocus") {
    mainSrc = baseUrl + "/video_feed_chase";
    subSrc = baseUrl + "/video_feed";
  } else if (viewMode === "frontFocus") {
    mainSrc = baseUrl + "/video_feed";
    subSrc = baseUrl + "/video_feed_chase";
  }

  return (
    <PreviewContainer>
      <MainImage viewMode={viewMode} src={mainSrc} alt="Main Camera" />
      <SecondaryImage viewMode={viewMode} src={subSrc} alt="Sub Camera" />
    </PreviewContainer>
  );
}
