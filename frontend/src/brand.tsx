/**
 * Luotsi marks from the brand kit (Luotsi-7a-Kit): the wordmark is Tussilago Light Italic outlined to paths, and
 * its i is a two-sail galley under a four-point star; the mark is that galley and star alone.
 * Inline rather than files so they take `currentColor` (teal on white, white on teal) and skip the kit's C2PA blocks.
 */
type MarkProps = { className?: string; label?: string };

const LETTERS =
  "M885 0L902-96L195-96L297-677L192-677L73 0ZM1594-213C1570-81 1488-62 1325-62C1179-62 1102-78 1102-170C1102-183 1103-197 1106-213L1164-546L1058-546L999-212C995-189 993-168 993-150C993-6 1111 15 1311 15C1535 15 1663-13 1700-213L1758-546L1652-546ZM2157 12C2440 12 2597-78 2597-330C2597-501 2472-558 2257-558C1974-558 1816-470 1816-216C1816-45 1941 12 2157 12ZM2243-484C2406-484 2495-438 2495-315C2495-132 2385-61 2169-61C2005-61 1915-107 1915-231C1915-414 2026-484 2243-484ZM2799 0L2884-483L2977-483L2988-546L2895-546L2918-677L2817-677L2794-546L2705-546L2694-483L2783-483L2698 0ZM3750-420C3734-495 3627-558 3423-558C3146-558 3029-486 3029-374C3029-259 3178-235 3286-235C3343-235 3395-238 3443-238C3536-238 3603-221 3603-166C3603-91 3527-50 3327-50C3174-50 3092-87 3072-155L2972-131C3000-30 3118 21 3306 21C3603 21 3707-63 3707-187C3707-280 3618-311 3467-311C3415-311 3360-308 3307-308C3235-308 3133-320 3133-387C3133-455 3228-490 3412-490C3552-490 3646-443 3654-391Z";

const GALLEY =
  "M3924.0 -546.0Q4090.4 -436.8 4059.3 -136.5L3884.6 -136.5Q3942.1 -338.5 3924.0 -546.0ZM4094.3 -458.6Q4238.8 -349.4 4223.1 -136.5L4081.1 -136.5Q4127.4 -305.8 4094.3 -458.6ZM3773.1 -185.6Q3800.4 -92.8 3866.9 -98.3L4282.9 -103.7Q4133.5 0.0 3894.2 -5.5Q3787.0 -16.4 3773.1 -185.6ZM3948.2 -777.0Q3963.2 -698.3 4042.0 -683.3Q3963.2 -668.3 3948.2 -589.5Q3933.2 -668.3 3854.5 -683.3Q3933.2 -698.3 3948.2 -777.0Z";

// Named when it stands alone; hidden from screen readers when a visible name sits next to it.
const a11y = (label?: string) => (label ? { role: "img", "aria-label": label } : { "aria-hidden": true as const });

/** The galley and star, cropped to the drawing (the kit's 512 px icon adds its own margin). */
export function LuotsiMark({ className = "", label }: MarkProps) {
  return (
    <svg viewBox="3763 -787 530 797" className={className} fill="currentColor" {...a11y(label)}>
      <path d={GALLEY} />
    </svg>
  );
}

export function LuotsiWordmark({ className = "", label = "Luotsi" }: MarkProps) {
  return (
    <svg viewBox="73 -777 4309.7 798" className={className} fill="currentColor" {...a11y(label)}>
      <path d={LETTERS + GALLEY} />
    </svg>
  );
}
