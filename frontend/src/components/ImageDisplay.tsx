/**
 * Defines a component that displays two images,
 * choosing which is the main vs secondary feed based on the viewMode.
 */
import React from "react";
import {
  PreviewContainer,
  MainImage,
  SecondaryImage,
} from "../styles/StyledImages";

type ImageDisplayProps = {
  viewMode: "sideBySide" | "frontFocus" | "chaseFocus";
};

export function ImageDisplay({ viewMode }: ImageDisplayProps) {
  // Decide which feed is main vs secondary.
  // You can customize the URLs or pass them in via props if needed.
  let mainSrc = "http://localhost:8000/video_feed";
  let subSrc = "http://localhost:8000/video_feed_chase";

  if (viewMode === "chaseFocus") {
    mainSrc = "http://localhost:8000/video_feed_chase";
    subSrc = "http://localhost:8000/video_feed";
  } else if (viewMode === "frontFocus") {
    mainSrc = "http://localhost:8000/video_feed";
    subSrc = "http://localhost:8000/video_feed_chase";
  }
  // sideBySide => The front camera is main, chase is sub

  return (
    <PreviewContainer>
      <MainImage viewMode={viewMode} src={mainSrc} alt="Main Camera" />
      <SecondaryImage viewMode={viewMode} src={subSrc} alt="Sub Camera" />
    </PreviewContainer>
  );
}
