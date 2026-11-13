"""Complete-entry correctness regressions from OPTIMIZATION_GOALS.md."""
import copy
import tempfile
import unittest
from unittest.mock import Mock, patch

from adbtv.adb import CmdResult
from adbtv.agent import Agent
from adbtv.config import DEFAULTS
from adbtv.device import TVDevice
from adbtv.perception import ScreenState
from adbtv.verifier import GoalSpec, Verifier


class CoreCorrectnessTests(unittest.TestCase):
    def device(self, audio, focus='com.trim.tv/.PlayerActivity', media=''):
        adb = Mock()
        def shell(args):
            output = {('dumpsys','window'): 'mCurrentFocus='+focus,
                      ('dumpsys','media_session'):media,
                      ('pidof','com.trim.tv'):'123',
                      ('dumpsys','audio'):audio}.get(tuple(args), '')
            return CmdResult(args,0,output,'')
        adb.shell.side_effect = shell
        return TVDevice(adb,DEFAULTS)

    def test_released_audio_track_is_not_playing_or_paused(self):
        for previous in ('started','paused','stopped'):
            with self.subTest(previous=previous):
                audio=f'new player piid:10 uid/pid:10054/123\nplayer piid:10 state:{previous}\nreleasing player piid:10\n'
                result=self.device(audio).playback_status(include_ui=False)
                self.assertFalse(result['playing'])
                self.assertFalse(result['evidence']['audio_track_paused'])

    def test_owned_new_track_survives_old_track_release(self):
        audio=('new player piid:10 uid/pid:10054/123\nplayer piid:10 state:started\n'
               'releasing player piid:10\nnew player piid:11 uid/pid:10054/123\n'
               'player piid:11 state:paused\n')
        state=self.device(audio).playback_status(include_ui=False)
        self.assertEqual(state['state'],'PAUSED')
        self.assertFalse(state['playing'])

    def test_verified_content_and_owned_player_audio_can_complete(self):
        goal=GoalSpec('play','com.trim.tv','山海归途',1,1)
        after=ScreenState(app='com.trim.tv',activity='.PlayerActivity',
                          texts=['山海归途','S01E001'],playback={
                              'package':'com.trim.tv','state':'PLAYING','playing':True,
                              'evidence':{'audio_track_started':True,'activity_is_player':True}})
        result=Verifier().verify(goal,None,after)
        self.assertEqual(result.status,'SUCCESS',result.to_dict())
        after.texts=['其他剧','S01E001']
        self.assertNotEqual(Verifier().verify(goal,None,after).status,'SUCCESS')
        after.texts=['山海归途','S01E010']
        self.assertNotEqual(Verifier().verify(goal,None,after).status,'SUCCESS')

    def test_explicit_title_survives_recommendation_word(self):
        agent=Agent(Mock(),Mock(),Mock(),DEFAULTS)
        goal=agent._goal('在哔哩哔哩找个《明确片名》播放')
        self.assertEqual(goal.title,'明确片名')

    def test_compound_goal_is_decomposed_not_truncated(self):
        # The whole request must be preserved as ordered steps; silently running
        # only the play prefix would be the failure this guards against.
        agent=Agent(Mock(),Mock(),Mock(),DEFAULTS)
        volume=agent._goal('打开飞牛播放《山海归途》并且调大音量')
        self.assertEqual(volume.action,'sequence')
        self.assertEqual([s.action for s in volume.steps],['play','volume_up'])
        self.assertEqual(volume.steps[0].title,'山海归途')
        delayed=agent._goal('打开飞牛播放《山海归途》十分钟后暂停')
        self.assertEqual(delayed.action,'sequence')
        self.assertEqual([s.action for s in delayed.steps],['play','delay'])
        self.assertEqual(delayed.steps[1].seconds,600.0)
        self.assertEqual(delayed.steps[1].steps[0].action,'pause')


if __name__=='__main__':
    unittest.main()
