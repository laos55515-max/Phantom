/** Inline SVG icon set (stroke: currentColor, 24×24 grid, feather-style). */

export interface IconProps {
  size?: number;
  className?: string;
  strokeWidth?: number;
  style?: React.CSSProperties;
}

function makeIcon(path: JSX.Element) {
  return function Icon({ size = 20, className, strokeWidth = 1.8, style }: IconProps) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={className}
        style={style}
        aria-hidden="true"
      >
        {path}
      </svg>
    );
  };
}

export const IconHide = makeIcon(
  <>
    <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
    <path d="M4 4l16 16" />
  </>,
);
export const IconExtract = makeIcon(
  <>
    <circle cx="8" cy="15" r="4.5" />
    <path d="M11.2 11.8 20 3" />
    <path d="M15.5 7.5 18 10" />
    <path d="M13 10l2 2" />
  </>,
);
export const IconSettings = makeIcon(
  <>
    <circle cx="12" cy="12" r="3.2" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.03-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.56-1.03 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h0a1.7 1.7 0 0 0 1.03-1.56V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56h0a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v0a1.7 1.7 0 0 0 1.56 1.03H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z" />
  </>,
);
export const IconFile = makeIcon(
  <>
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <path d="M14 2v6h6" />
  </>,
);
export const IconText = makeIcon(
  <>
    <path d="M4 7V5h16v2" />
    <path d="M9 5v14" />
    <path d="M12 19h-6" />
    <path d="M15 12h5" />
    <path d="M15 16h5" />
    <path d="M15 8h5" />
  </>,
);
export const IconSparkle = makeIcon(
  <>
    <path d="M12 3l1.9 4.6 4.6 1.9-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z" />
    <path d="M19 14l.9 2.1L22 17l-2.1.9L19 20l-.9-2.1L16 17l2.1-.9z" />
  </>,
);
export const IconImage = makeIcon(
  <>
    <rect x="3" y="4" width="18" height="16" rx="2.5" />
    <circle cx="9" cy="10" r="1.6" />
    <path d="M21 15l-4.5-4.5L9 18" />
  </>,
);
export const IconAudio = makeIcon(
  <>
    <path d="M9 18V6l10-2v11.5" />
    <circle cx="6.8" cy="18" r="2.6" />
    <circle cx="16.8" cy="15.5" r="2.6" />
  </>,
);
export const IconChevronRight = makeIcon(<path d="M9 6l6 6-6 6" />);
export const IconChevronLeft = makeIcon(<path d="M15 6l-6 6 6 6" />);
export const IconLock = makeIcon(
  <>
    <rect x="4.5" y="10.5" width="15" height="9.5" rx="2.5" />
    <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" />
  </>,
);
export const IconEye = makeIcon(
  <>
    <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
    <circle cx="12" cy="12" r="3" />
  </>,
);
export const IconEyeOff = makeIcon(
  <>
    <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
    <circle cx="12" cy="12" r="3" />
    <path d="M4 4l16 16" />
  </>,
);
export const IconDice = makeIcon(
  <>
    <rect x="3" y="3" width="18" height="18" rx="4" />
    <circle cx="8.5" cy="8.5" r="0.5" fill="currentColor" strokeWidth="1.6" />
    <circle cx="15.5" cy="15.5" r="0.5" fill="currentColor" strokeWidth="1.6" />
    <circle cx="15.5" cy="8.5" r="0.5" fill="currentColor" strokeWidth="1.6" />
    <circle cx="8.5" cy="15.5" r="0.5" fill="currentColor" strokeWidth="1.6" />
    <circle cx="12" cy="12" r="0.5" fill="currentColor" strokeWidth="1.6" />
  </>,
);
export const IconCheck = makeIcon(<path d="M4 12.5l5.5 5.5L20 7" />);
export const IconCheckCircle = makeIcon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M8.5 12.5l2.5 2.5 4.5-5" />
  </>,
);
export const IconAlert = makeIcon(
  <>
    <path d="M12 3.5 2.5 20h19z" />
    <path d="M12 10v4.5" />
    <circle cx="12" cy="17" r="0.4" fill="currentColor" strokeWidth="1.5" />
  </>,
);
export const IconInfo = makeIcon(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5" />
    <circle cx="12" cy="8" r="0.4" fill="currentColor" strokeWidth="1.5" />
  </>,
);
export const IconCopy = makeIcon(
  <>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h10" />
  </>,
);
export const IconSun = makeIcon(
  <>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8" />
  </>,
);
export const IconMoon = makeIcon(
  <path d="M20 13.5A8.5 8.5 0 0 1 10.5 4 7 7 0 1 0 20 13.5z" />,
);
export const IconMonitor = makeIcon(
  <>
    <rect x="2.5" y="4" width="19" height="13" rx="2" />
    <path d="M9 21h6M12 17v4" />
  </>,
);
export const IconFolder = makeIcon(
  <path d="M3.5 7a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" />,
);
export const IconShield = makeIcon(
  <>
    <path d="M12 3l7.5 3v6c0 4.6-3.2 7.9-7.5 9-4.3-1.1-7.5-4.4-7.5-9V6z" />
    <path d="M9.5 12l2 2 3.5-4" />
  </>,
);
export const IconSpinner = makeIcon(
  <path d="M12 3a9 9 0 1 0 9 9" />,
);
export const IconPdf = makeIcon(
  <>
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <path d="M14 2v6h6" />
    <text x="8.2" y="17.4" fontSize="6.5" fontWeight="800" stroke="none" fill="currentColor">
      PDF
    </text>
  </>,
);
