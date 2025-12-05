"""Theme and styling definitions for the web UI."""

# AIRI theme overrides for Gradio
AIRI_CSS = """
:root {
    --primary-50: #f0fdfb !important;
    --primary-100: #d4f8f4 !important;
    --primary-200: #a9f1e9 !important;
    --primary-300: #7de9dd !important;
    --primary-400: #52d5c9 !important;
    --primary-500: #13c1ac !important;
    --primary-600: #109a8a !important;
    --primary-700: #0d7468 !important;
    --primary-800: #0a4d46 !important;
    --primary-900: #072724 !important;
    --primary-950: #041614 !important;
}
:root .dark {
    --primary-100: #d4f8f4 !important;
    --primary-200: #a9f1e9 !important;
    --primary-300: #7de9dd !important;
    --primary-400: #52d5c9 !important;
    --primary-500: #13c1ac !important;
    --primary-600: #109a8a !important;
    --primary-700: #0d7468 !important;
    --primary-800: #0a4d46 !important;
    --primary-900: #072724 !important;
    --primary-950: #041614 !important;
}
:root, :root .dark {
    --secondary-50: #faf8f5 !important;
    --secondary-100: #f4efe8 !important;
    --secondary-200: #e9dccf !important;
    --secondary-300: #dcc4ab !important;
    --secondary-400: #d6b594 !important;
    --secondary-500: #d1a582 !important;
    --secondary-600: #c08a61 !important;
    --secondary-700: #a57251 !important;
    --secondary-800: #885e45 !important;
    --secondary-900: #6f4e3a !important;
    --secondary-950: #3b291e !important;
}
footer {visibility: hidden}

.short-upload .icon-wrap {
  display: none;
}
.short-upload button div.wrap {
  padding-top: 0 !important;
}

/* Light theme colors */
@media (prefers-color-scheme: light) {
  :root {
    /* Background */
    --body-background-fill: #f2eee8ff;
  }
}

/* Dark theme colors */
@media (prefers-color-scheme: dark) {
  :root {
    /* Background - use default dark theme */
    --body-background-fill: unset;
  }
}

/* Universal brand colors for both themes */
:root {
  /* Primary buttons */
  --button-primary-background-fill: #13c1acff;
  --button-primary-background-fill-hover: #13c1acff;
  --button-primary-text-color: #ffffff;

  /* Secondary buttons */
  --button-secondary-background-fill: #8c939cff;
  --button-secondary-background-fill-hover: #8c939cff;
  --button-secondary-text-color: #ffffff;

  /* Stop/Danger buttons (variant="stop") */
  --button-cancel-background-fill: #e96c6cff;
  --button-cancel-background-fill-hover: #e96c6cff;
  --button-cancel-text-color: #ffffff;
  /* Additional aliases used by some Gradio themes */
  --button-danger-background-fill: #e96c6cff;
  --button-danger-background-fill-hover: #e96c6cff;
  --button-danger-text-color: #ffffff;
  --color-error: #e96c6cff;

  /* Tabs */
  --tab-active-background-fill: #13c1acff;
  --tab-active-text-color: #ffffff;

  /* Global accent for sliders and focused controls */
  --color-accent: #13c1acff;
}

/* Ensure page background follows the brand color, but respect dark mode preference */
@media (prefers-color-scheme: light) {
  body, .gradio-container {
    background: #f2eee8ff !important;
  }
}

/* Active tab fallback (Gradio v4 tabs are buttons with role="tab") */
button[role="tab"][aria-selected="true"] {
  background: #13c1acff !important;
  color: #ffffff !important;
}

/* Sliders: broad support via accent-color */
input[type="range"] {
  accent-color: #13c1acff;
}

/* Preset Buttons Styling - Ensure buttons display in a nice row */
div.gradio-container .gr-row:has(.gr-button) {
  gap: 0.5rem !important;
  flex-wrap: wrap !important;
}

.preset-button {
  min-width: fit-content !important;
  max-width: 200px !important;
}

/* Fallbacks to ensure stop/danger buttons are painted even if variables differ */
button.stop, .gr-button.stop, .gr-button[class*="stop"], button[class*="stop"] {
  background: #e96c6cff !important;
  color: #ffffff !important;
  border-color: transparent !important;
}
button.stop:hover, .gr-button.stop:hover, .gr-button[class*="stop"]:hover, button[class*="stop"]:hover {
  background: #e96c6cff !important;
  color: #ffffff !important;
}

/* Dark theme button color overrides to ensure brand colors persist */
@media (prefers-color-scheme: dark) {
  /* Force primary buttons to maintain brand color in dark theme */
  button.primary, button.btn-primary, .gr-button[variant="primary"] {
    background: #13c1acff !important;
    color: #ffffff !important;
    border-color: transparent !important;
  }

  button.primary:hover, button.btn-primary:hover, .gr-button[variant="primary"]:hover {
    background: #13c1acff !important;
    color: #ffffff !important;
    filter: brightness(0.9);
  }

  /* Force secondary buttons to maintain brand color in dark theme */
  button.secondary, button.btn-secondary, .gr-button[variant="secondary"] {
    background: #8c939cff !important;
    color: #ffffff !important;
    border-color: transparent !important;
  }

  button.secondary:hover, button.btn-secondary:hover, .gr-button[variant="secondary"]:hover {
    background: #8c939cff !important;
    color: #ffffff !important;
    filter: brightness(0.9);
  }

  /* Ensure stop/danger buttons maintain color in dark theme */
  button.stop, .gr-button.cancel, .gr-button.danger,
  button[variant="stop"], button[variant="cancel"], button[variant="danger"] {
    background: #e96c6cff !important;
    color: #ffffff !important;
    border-color: transparent !important;
  }

  .gr-button.stop:hover, .gr-button.cancel:hover, .gr-button.danger:hover,
  button[variant="stop"]:hover, button[variant="cancel"]:hover, button[variant="danger"]:hover {
    background: #e96c6cff !important;
    color: #ffffff !important;
    filter: brightness(0.9);
  }

  /* Ensure active tabs maintain brand color in dark theme */
  button[role="tab"][aria-selected="true"] {
    background: #13c1acff !important;
    color: #ffffff !important;
  }
  button[role="tab"][aria-selected="true"]::after, button.selected::after {
    background-color: #13c1acff !important;
  }
  button.selected {
    color: #13c1acff !important;
  }
  .overflow-item-selected svg {
    color: #13c1acff !important;
  }
}
"""
