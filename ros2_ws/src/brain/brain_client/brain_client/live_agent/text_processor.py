#!/usr/bin/env python3
"""Text processing utilities for LiveAgent speech synthesis."""

import re
from typing import Tuple


class TextProcessor:
    """Handles text buffering and sentence extraction for speech synthesis."""
    
    SENTENCE_PATTERN = r'([.!?]+(?:\s+|$))'  # Match punctuation followed by whitespace OR end of string
    THINKING_PATTERN = r'<thinking>.*?</thinking>'
    
    @staticmethod
    def clean_text(text: str) -> str:
        """
        Clean text by removing unwanted content like thinking blocks.
        
        Args:
            text: Raw text that may contain thinking blocks
            
        Returns:
            Cleaned text with thinking blocks removed
        """
        cleaned = re.sub(TextProcessor.THINKING_PATTERN, '', text, flags=re.DOTALL)
        return cleaned
    
    @staticmethod
    def extract_complete_sentences(buffer: str, new_text: str) -> Tuple[str, str]:
        """
        Extract complete sentences from buffered text.
        
        Splits text on sentence boundaries (. ! ?) and returns complete sentences
        along with any incomplete remaining text. Also cleans out thinking blocks.
        
        Args:
            buffer: Previously buffered incomplete text
            new_text: New text to add to buffer
            
        Returns:
            Tuple of (complete_sentences, remaining_buffer)
            
        Example:
            >>> TextProcessor.extract_complete_sentences("Hello", " world. How are")
            ("Hello world. ", "How are")
        """
        new_text = TextProcessor.clean_text(new_text)
        buffer += new_text
        sentences = re.split(TextProcessor.SENTENCE_PATTERN, buffer)
        
        complete_text = ""
        
        # Process pairs (sentence + delimiter)
        for i in range(0, len(sentences) - 1, 2):
            if i + 1 < len(sentences):
                sentence = sentences[i] + sentences[i + 1]
                if sentence.strip():
                    complete_text += sentence
        
        # Last item is the incomplete part (no delimiter after it)
        remaining = sentences[-1] if sentences else ""
        
        return complete_text, remaining

