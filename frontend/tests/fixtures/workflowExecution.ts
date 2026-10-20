// 只读现场形状：父流程停留在同一阶段54分钟，子Job仍逐句推进；无业务网络请求。
export function runningWorkflowStep() {
  const run = { id:'16202bcb2d4646dbaabd7dbd882f93d2',project_uid:'fixture',status:'running',cursor:5,total:8,phase:'字幕与配音',mode:'publish',
    execution:{schema:1,owner:'workflow',run_id:'16202bcb2d4646dbaabd7dbd882f93d2',state:'running',phase:'production.tts',label:'字幕与配音',
      completed:5,total:8,unit:'制作步骤',metrics:{},started_at:'2026-10-19T20:54:10.416+00:00',updated_at:'2026-10-19T20:54:11.870+00:00',
      elapsed_seconds:3255.1,inactive_seconds:3253.7,finished_at:null,
      history:[{phase:'production.translate',label:'翻译',state:'completed',started_at:'2026-10-19T19:42:45.919+00:00',finished_at:'2026-10-19T19:46:26.174+00:00'},
        {phase:'production.tts',label:'字幕与配音',state:'running',started_at:'2026-10-19T20:54:11.870+00:00',finished_at:null}]} }
  const plan={id:run.id,steps:['download','preprocess','transcribe','split','translate','tts','comments','assemble']}
  const step={key:'tts',project_uid:'fixture',label:'字幕与配音',status:'running',job_id:520,progress:70.5,eta_seconds:1938,eta_scope:'step',
    execution:{schema:1,owner:'step',job_id:520,state:'running',phase:'synthesizing',label:'处理第 454 句配音',detail:'',
      completed:453,total:646,unit:'句',started_at:'2026-10-19T20:54:12.680+00:00',updated_at:'2026-10-19T21:48:25.474+00:00',
      finished_at:null,elapsed_seconds:3252.9,inactive_seconds:0.1,metrics:{current_id:454,sentence_elapsed_seconds:10.5,sentence_progress:0.5},
      history:[{phase:'synthesizing',label:'处理第 454 句配音',state:'running',started_at:'2026-10-19T20:54:15.422+00:00',finished_at:null}]} }
  return {run,plan,step}
}
