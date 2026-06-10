<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";
import type { Row } from "../api";

const props = defineProps<{ series?: Row[]; benchmarkTicker?: string }>();

const el = ref<HTMLDivElement>();
let chart: IChartApi | null = null;
let themeObserver: MutationObserver | null = null;

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function hasOhlc(series: Row[]): boolean {
  return series.every((bar) => bar.open != null && bar.high != null && bar.low != null);
}

function render() {
  if (chart) {
    chart.remove();
    chart = null;
  }
  const series = props.series ?? [];
  if (!el.value || series.length < 2) return;

  const up = cssVar("--up") || "#ff5d5d";
  const down = cssVar("--down") || "#28c98a";
  const flat = cssVar("--flat") || "#97a5b9";
  const ink = cssVar("--ink") || "#e9eff7";
  const ink3 = cssVar("--ink-3") || "#5c697c";
  const line = cssVar("--line") || "#232d3e";

  chart = createChart(el.value, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: ink3, fontSize: 11 },
    grid: { vertLines: { color: line }, horzLines: { color: line } },
    rightPriceScale: { borderColor: line },
    leftPriceScale: { visible: true, borderColor: line },
    timeScale: { borderColor: line, fixLeftEdge: true, fixRightEdge: true },
    crosshair: { horzLine: { color: flat }, vertLine: { color: flat } },
    handleScroll: false,
    handleScale: false,
  });

  // 标的：有完整 OHLC 时画日线蜡烛（雪球 kline），否则退回收盘折线（CSV 仅收盘）。
  let asset: ISeriesApi<"Candlestick"> | ISeriesApi<"Line">;
  if (hasOhlc(series)) {
    asset = chart.addCandlestickSeries({
      priceScaleId: "right",
      upColor: up,
      downColor: down,
      borderUpColor: up,
      borderDownColor: down,
      wickUpColor: up,
      wickDownColor: down,
    });
    asset.setData(
      series.map((bar) => ({
        time: bar.date as Time,
        open: Number(bar.open),
        high: Number(bar.high),
        low: Number(bar.low),
        close: Number(bar.close),
      })),
    );
  } else {
    asset = chart.addLineSeries({ priceScaleId: "right", color: ink, lineWidth: 2 });
    asset.setData(series.map((bar) => ({ time: bar.date as Time, value: Number(bar.close) })));
  }

  // 基准（如 SH000300）走独立左轴,只为对照走势,不与标的价位混轴。
  const benchmark = chart.addLineSeries({
    priceScaleId: "left",
    color: flat,
    lineWidth: 1,
    lineStyle: 2,
    priceLineVisible: false,
    lastValueVisible: false,
  });
  benchmark.setData(
    series
      .filter((bar) => bar.benchmark_close != null)
      .map((bar) => ({ time: bar.date as Time, value: Number(bar.benchmark_close) })),
  );

  chart.timeScale().fitContent();
}

onMounted(() => {
  render();
  themeObserver = new MutationObserver(render);
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
});
watch(() => props.series, render, { deep: true });
onBeforeUnmount(() => {
  themeObserver?.disconnect();
  chart?.remove();
  chart = null;
});
</script>

<template>
  <figure v-if="(series?.length ?? 0) >= 2" class="market-chart">
    <div ref="el" class="market-chart-canvas"></div>
    <figcaption class="muted">
      日线 · 红涨绿跌 · 蜡烛为标的，虚线为基准<span v-if="benchmarkTicker"> {{ benchmarkTicker }}</span>（独立坐标）
    </figcaption>
  </figure>
</template>
