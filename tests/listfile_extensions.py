"""Every file extension World of Warcraft actually ships, and how many.

Measured from the community listfile, which names every file in every build
Blizzard has published:

    https://github.com/wowdev/wow-listfile
    community-listfile.csv, 2,218,381 entries, retrieved 2026-09-14

The point of checking this in is that the extension tables in ``detect`` stop
being a matter of recollection.  A format can be added to them only if it is
here, and every format here has to be decided on -- which is what
``tests/test_detect.py`` asserts, in both directions.

Counts are files, not bytes.  An empty key is a file whose name has no
extension at all.
"""

#: ``extension -> (file count, one real path)``.
LISTFILE_EXTENSIONS = {
    '.blp'        : ( 848410, 'character/bloodelf/female/bloodelffemalefacelower00_00.blp'),
    '.adt'        : ( 340539, 'world/maps/ahnqiraj/ahnqiraj_26_46.adt'),
    '.skin'       : ( 304358, 'cameras/orcintro0500.skin'),
    '.ogg'        : ( 266664, 'sound/ambience/wmoambience/ahnqirajinterirorfireflyroom.ogg'),
    '.m2'         : ( 134241, 'cameras/flybybloodelf.m2'),
    '.wmo'        : (  96568, 'world/wmo/azeroth/buildings/altarofstorms/altarofstorms.wmo'),
    '.meta'       : (  77846, 'dungeons/textures/trim/mm_strmwnd_int_trim_01.blp.meta'),
    '.anim'       : (  51925, 'character/bloodelf/female/bloodelffemale0081-00.anim'),
    '.pm4'        : (  20857, 'world/maps/development/development_25_19.pm4'),
    '.mdx'        : (  12765, 'character/dwarf/female/dwarffemale.mdx'),
    '.dat'        : (   9786, 'world/maps/ahnqiraj/area_26_46.dat'),
    '.lua'        : (   7284, 'interface/addons/blizzard_achievementui/blizzard_achievementui.l'),
    '.bls'        : (   7128, 'shaders/domain/ds_5_0/model2displ_t1.bls'),
    '.mp3'        : (   6698, 'sound/music/citymusic/darnassus/darnassus intro.mp3'),
    '.pd4'        : (   5555, 'world/wmo/cameron.pd4'),
    '.wdt'        : (   5079, 'world/maps/ahnqiraj/ahnqiraj.wdt'),
    '.bone'       : (   3424, 'character/draenei/female/draeneifemale_hd_03.bone'),
    '.xml'        : (   3245, 'interface/addons/blizzard_achievementui/blizzard_achievementui.x'),
    '.unk'        : (   2763, 'world/maps/ashran/2816676.unk'),
    '.wtf'        : (   2217, 'wtf/rov.wtf'),
    '.db2'        : (   1300, 'dbfilesclient/battlepetabilityeffect.db2'),
    '.tex'        : (   1085, 'world/liquid.tex'),
    '.wdl'        : (   1073, 'world/maps/ahnqiraj/ahnqiraj.wdl'),
    '.phys'       : (    964, 'item/objectcomponents/waist/buckle_panstart_a_01.phys'),
    '.toc'        : (    911, 'interface/addons/blizzard_achievementui/blizzard_achievementui.t'),
    '.wwe'        : (    770, 'world/wmo/brokenisles/suramar/7sr_catacombs_micro_large04.wwe'),
    '.png'        : (    750, 'wowedit/uitextureatlas/atlasimage0000000088.png'),
    '.col'        : (    608, 'world/maps/assualtonstormwind/assualtonstormwind_29_46.col'),
    '.avi'        : (    552, 'interface/cinematics/logo_800.avi'),
    '.wlw'        : (    418, 'world/maps/ahnqiraj/ahnqiraj - aquaduct 01.wlw'),
    '.dbc'        : (    401, 'dbfilesclient/animkit.dbc'),
    '.h2o'        : (    359, 'world/maps/alittlepatiencescenario/liquid3.h2o'),
    '.strings'    : (    242, 'world of warcraft.app/contents/resources/en.lproj/startup.string'),
    '.scn'        : (    227, 'dbfilesclient/declinedword.scn'),
    '.txt'        : (    171, 'component.wow-enus.txt'),
    '.bin'        : (    150, 'utils/snapshot_blob.bin'),
    '.skel'       : (    131, 'character/draenei/male/draeneimale_hd.skel'),
    '.sbt'        : (    124, 'interface/cinematics/wow_intro_lk.sbt'),
    '.tga'        : (    111, 'interface/addons/designermenu3/quickdb/map.tga'),
    '.pvdata'     : (     64, 'environments/particulatevolumes/pvdata/test1.pvdata'),
    ''            : (     57, 'signaturefile'),
    '.wwf'        : (     55, 'particles/particulates/weather440_2999110.wwf'),
    '.sig'        : (     53, 'interface/addons/blizzard_arenaui/blizzard_arenaui.toc.sig'),
    '.amsfolder'  : (     50, 'interface/cinematics/test/.amsfolder'),
    '.srt'        : (     41, 'interface/cinematics/subtitletest_dragonflight_ptw.srt'),
    '.wlm'        : (     39, 'world/maps/azeroth/bralter.wlm'),
    '.zmp'        : (     38, 'interface/worldmap/gilneas_terrain2.zmp'),
    '.dll'        : (     38, 'dbghelp.dll'),
    '.pak'        : (     30, 'utils/locales/ko.pak'),
    '.lst'        : (     27, 'triallists/startbloodelf.lst'),
    '.icns'       : (     26, 'world of warcraft test.app/contents/resources/wow-test.icns'),
    '.nib'        : (     23, 'world of warcraft test.app/contents/resources/applicationmenu.ni'),
    '.ttf'        : (     20, 'fonts/arialn.ttf'),
    '.html'       : (     20, 'tos.html'),
    '.plist'      : (     20, 'world of warcraft test.app/contents/info.plist'),
    '.exe'        : (     19, 'blizzarderror.exe'),
    '.dylib'      : (     16, 'updateplugin_old.dylib'),
    '.m3'         : (     10, 'models/unknown/unk_exp10_5569152/5569152.m3'),
    '.mtl3lib'    : (      9, 'models/unknown/unk_exp10_5916032/5916032_6099349.mtl3lib'),
    '.wfx'        : (      6, 'shaders/effects/litsphere.wfx'),
    '.what'       : (      3, 'world/maps/icecrowncitadel/area_27_25.what'),
    '.blob'       : (      3, 'world/model.blob'),
    '.xsd'        : (      3, 'interface/framexml/ui.xsd'),
    '.manifest'   : (      2, 'clientmanifest.manifest'),
    '.url'        : (      2, 'techsupport.url'),
    '.json'       : (      2, 'utils/blizzardbrowser.app/contents/frameworks/chromium embedded '),
    '.wav'        : (      1, 'sound/soundtest04.wav'),
    '.delete'     : (      1, 'world/maps/development/dungeon.chk.delete'),
    '.ini'        : (      1, 'wow.ini'),
    '.csp'        : (      1, 'character/orc/male/a.csp'),
    '.signed'     : (      1, 'ca_bundle.txt.signed'),
    '.htm'        : (      1, 'credits_ct.htm'),
}

#: Total named files the counts were taken from.
LISTFILE_TOTAL = 2218381
