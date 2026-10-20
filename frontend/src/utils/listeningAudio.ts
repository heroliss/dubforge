/** 播放器本机监听状态；不属于项目混音参数，也不进入制作指纹。 */
export type ListeningAudioState = { volume: number; muted: boolean }

export function listeningAudioState(value: ListeningAudioState): ListeningAudioState {
  return { volume: Number.isFinite(value.volume) ? Math.max(0, Math.min(1, value.volume)) : 1,
    muted: !!value.muted }
}
