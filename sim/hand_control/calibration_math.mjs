import { median } from "../../webapp/js/handControl/math.js";

export { median };
export const vectorMedian = (rows) =>
  rows[0].map((_, i) => median(rows.map((row) => row[i])));

function solve(a, b) {
  a = a.map((row, i) => [...row, b[i]]);
  for (let i = 0; i < a.length; i++) {
    let pivot = i;
    for (let j = i + 1; j < a.length; j++)
      if (Math.abs(a[j][i]) > Math.abs(a[pivot][i])) pivot = j;
    [a[i], a[pivot]] = [a[pivot], a[i]];
    if (Math.abs(a[i][i]) < 1e-10)
      throw new Error("The taught directions cannot be separated reliably.");
    const divisor = a[i][i];
    a[i] = a[i].map((v) => v / divisor);
    for (let j = 0; j < a.length; j++) {
      if (j === i) continue;
      const factor = a[j][i];
      a[j] = a[j].map((v, k) => v - factor * a[i][k]);
    }
  }
  return a.map((row) => row.at(-1));
}

export function ridge(x, y, lambda, weights = x.map(() => 1)) {
  const n = x[0].length;
  const gram = Array.from({ length: n }, (_, i) =>
    Array.from(
      { length: n },
      (_, j) =>
        x.reduce((s, row, k) => s + weights[k] * row[i] * row[j], 0) +
        (i === j ? lambda : 0),
    ),
  );
  return y[0].map((_, output) =>
    solve(
      gram,
      Array.from({ length: n }, (_, i) =>
        x.reduce((s, row, k) => s + weights[k] * row[i] * y[k][output], 0),
      ),
    ),
  );
}
