// One composition, "Join": the tail of shot A turns into the head of shot B with a chosen transition.
// fx.py renders many of these in one run (render.mjs), each with its own props.
import {AbsoluteFill, Composition, OffthreadVideo, staticFile} from 'remotion';
import {linearTiming, springTiming, TransitionSeries} from '@remotion/transitions';
import {LightLeak} from '@remotion/light-leaks';
import {bookFlip} from '@remotion/transitions/book-flip';
import {clockWipe} from '@remotion/transitions/clock-wipe';
import {crossZoom} from '@remotion/transitions/cross-zoom';
import {dissolve} from '@remotion/transitions/dissolve';
import {dreamyZoom} from '@remotion/transitions/dreamy-zoom';
import {fade} from '@remotion/transitions/fade';
import {filmBurn} from '@remotion/transitions/film-burn';
import {iris} from '@remotion/transitions/iris';
import {linearBlur} from '@remotion/transitions/linear-blur';
import {pushCut} from '@remotion/transitions/push-cut';
import {ripple} from '@remotion/transitions/ripple';
import {slide} from '@remotion/transitions/slide';
import {wipe} from '@remotion/transitions/wipe';
import {zoomBlur} from '@remotion/transitions/zoom-blur';

const W = 1920, H = 1080;

const KINDS = {
  'book-flip': () => bookFlip({}),
  'clock-wipe': () => clockWipe({width: W, height: H}),
  'cross-zoom': () => crossZoom({}),
  dissolve: () => dissolve({}),
  'dreamy-zoom': () => dreamyZoom({}),
  fade: () => fade(),
  'film-burn': (seed) => filmBurn({seed}),
  iris: () => iris({width: W, height: H}),
  'linear-blur': () => linearBlur({}),
  'push-cut': () => pushCut({}),
  ripple: () => ripple({}),
  'slide': () => slide({direction: 'from-right'}),
  wipe: () => wipe({direction: 'from-left'}),
  'zoom-blur': () => zoomBlur({}),
};

const Clip = ({src}) => (
  <AbsoluteFill style={{backgroundColor: 'black'}}>
    <OffthreadVideo src={src.startsWith('http') || src.startsWith('/') ? src : staticFile(src)} muted />
  </AbsoluteFill>
);

export const Join = ({a, b, kind, frames, seed, leak, hue}) => {
  const make = KINDS[kind] || KINDS.fade;
  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      <TransitionSeries>
        <TransitionSeries.Sequence durationInFrames={frames}>
          <Clip src={a} />
        </TransitionSeries.Sequence>
        <TransitionSeries.Transition presentation={make(seed)} timing={linearTiming({durationInFrames: frames})} />
        <TransitionSeries.Sequence durationInFrames={frames}>
          <Clip src={b} />
        </TransitionSeries.Sequence>
      </TransitionSeries>
      {leak ? <LightLeak durationInFrames={frames} seed={seed} hueShift={hue} /> : null}
    </AbsoluteFill>
  );
};

// Just the light leak over a transparent frame, laid over the film by ffmpeg at a turn of the story.
export const Leak = ({frames, seed, hue}) => <LightLeak durationInFrames={frames} seed={seed} hueShift={hue} />;

export const Root = () => (
  <>
    <Composition
      id="Join"
      component={Join}
      width={W}
      height={H}
      fps={30}
      durationInFrames={24}
      defaultProps={{a: 'a.mp4', b: 'b.mp4', kind: 'fade', frames: 24, seed: 2.31, leak: false, hue: 0}}
      calculateMetadata={({props}) => ({durationInFrames: props.frames})}
    />
    <Composition
      id="Leak"
      component={Leak}
      width={W}
      height={H}
      fps={30}
      durationInFrames={30}
      defaultProps={{frames: 30, seed: 1, hue: 0}}
      calculateMetadata={({props}) => ({durationInFrames: props.frames})}
    />
  </>
);
