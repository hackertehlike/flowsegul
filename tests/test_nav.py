"""Map navigation: click a call to reach its box, and find in the current map.

Run: python3 -m unittest discover tests
"""
import os, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, '..', 'scripts', 'template.html')


class MapNav(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TEMPLATE) as f:
            cls.t = f.read()

    def test_mouse_focus_does_not_glide_to_the_box_you_are_in(self):
        # focusing a box by clicking inside it used to glide to that box first,
        # sliding the call name away before the click landed
        self.assertIn("world.addEventListener('pointerdown',()=>{ptrFocus=Date.now();},true);", self.t)
        self.assertIn("Date.now()-ptrFocus<600)return;", self.t)

    def test_call_jump_can_go_back(self):
        self.assertIn("h.kind='view';xhist.push(h);", self.t)
        self.assertIn("h.kind==='view'", self.t)

    def test_page_has_find_in_map(self):
        for needle in ('id="mfind"', 'id="mq"', 'id="mqn"', 'id="mqprev"', 'id="mqnext"',
                       'function mfRun', 'function mfStep', 'function mfShow',
                       "(e.key==='f'||e.key==='F')"):
            self.assertIn(needle, self.t)

    def test_sidebar_can_be_resized(self):
        for needle in ('id="sgrip"', 'var(--sidew,252px)', "store.set('sidew'"):
            self.assertIn(needle, self.t)


if __name__ == '__main__':
    unittest.main()
