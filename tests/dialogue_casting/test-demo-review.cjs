'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {validateDemoReview, snapshotDemoReview, pendingDemoReview, changedDemoMachine} = require('../../scripts/dialogue_casting/demo-review.js');

const data = {demo_id:'demo', experiment_id:'experiment'};
const all = new Map([
  ['cue-a', {clip_sha256:'audio-a', machine:{label:'female'}}],
  ['cue-b', {clip_sha256:'audio-b', machine:{label:'unknown'}}]
]);

test('malformed null documents and rows have a clear validation error', () => {
  assert.throws(()=>validateDemoReview(null, data, all), /Tệp/);
  const doc = snapshotDemoReview(data, all, {});
  doc.reviews = [null];
  assert.throws(()=>validateDemoReview(doc, data, all), /Dòng nhãn/);
});

test('export/import retains automatic reset, manual correction and literal notes', () => {
  const reviews = {
    'cue-a':{override:'male', reviewed:true, notes:'Nghe lại <script>không thực thi</script>'},
    'cue-b':{override:null, reviewed:false, notes:''}
  };
  const exported = JSON.parse(JSON.stringify(snapshotDemoReview(data, all, reviews)));
  assert.deepEqual(validateDemoReview(exported, data, all), reviews);
  assert.equal(exported.label_source, 'demo-assisted-review');
  assert.equal(all.get('cue-a').machine.label, 'female');
});

for (const mutation of ['dataset', 'experiment', 'source', 'duplicate', 'clip', 'machine', 'label', 'notes', 'reviewed', 'unknown']) {
  test('import refuses '+mutation+' without changing the current review state', () => {
    const reviews = {'cue-a':{override:'male', reviewed:true, notes:'giữ lại'}};
    const document = snapshotDemoReview(data, all, reviews);
    const before = structuredClone(reviews);
    if (mutation==='dataset') document.demo_id='other';
    if (mutation==='experiment') document.experiment_id='other';
    if (mutation==='source') document.label_source='human-listening';
    if (mutation==='duplicate') document.reviews.push(structuredClone(document.reviews[0]));
    if (mutation==='clip') document.reviews[0].clip_sha256='changed';
    if (mutation==='machine') document.reviews[0].machine_label='male';
    if (mutation==='label') document.reviews[0].override='unsupported';
    if (mutation==='notes') document.reviews[0].notes=12;
    if (mutation==='reviewed') document.reviews[0].reviewed='yes';
    if (mutation==='unknown') document.reviews[0].cue_id='missing';
    assert.throws(()=>validateDemoReview(document, data, all));
    assert.deepEqual(reviews, before);
  });
}

test('pending review uses effective label and preserves reviewed Automatic observations', () => {
  const cue={id:'a', machine:{label:'unknown'}};
  assert.equal(pendingDemoReview(cue,{}),true);
  assert.equal(pendingDemoReview(cue,{a:{override:'male',reviewed:true}}),false);
  assert.equal(pendingDemoReview(cue,{a:{override:null,reviewed:true,notes:'chỉ có nhạc'}}),false);
  assert.equal(pendingDemoReview(cue,{a:{override:'unknown',reviewed:false}}),true);
  assert.equal(pendingDemoReview({id:'a',machine:{label:'male'}},{}),false);
});

test('model comparison filter distinguishes raw machine changes from manual corrections', () => {
  assert.equal(changedDemoMachine({machine:{label:'male'}}),false);
  assert.equal(changedDemoMachine({machine:{label:'male'},comparison_machine:{label:'male'}}),false);
  assert.equal(changedDemoMachine({machine:{label:'unknown'},comparison_machine:{label:'female'}}),true);
  assert.equal(changedDemoMachine({machine:{label:'male'},comparison_machine:{label:'unknown'}}),true);
});
