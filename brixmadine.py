'''Convert a Dark Forces .LEV map (along with its attached resources) into a LEGO .ldr model to be edited in e.g. MLCAD
or BrickLink studio, or the like.

* Pixel stretching factor is approx 1.2:1 in the original level(s) (a square actually is 1.2 tall and 1 wide)
* Horizontal stability is more important than vertical stability in LEGO. I.e. it is more important to stabilize and
  optimize the bricks horizontally first, and vertically afterward
  * For the same reason, it is better to start with the walls and overwrite with any floor plates later as there is
  higher likelihood they will be in similar colors and therefore tesselated together

====

General approach:
1) Load the .LEV
2) Check for the matching .PAL palette
3) Locate all the .BM's (or optionally .BMP's if already there)
4) Parse the LEV
5) Sort the sectors by size ascending or descending (either prioritize detail or homogeneity)
6) For each sector, set up its floor as the trace of 1x1 plates, and optionally the ceiling, considering the floor
   and/or ceiling .BM's
7) Trace walls, too, considering BM's as well, and taking into account adjoins, with 1x1 plates of nearest or
   antialiased color.
8) Optionally add support beams and reinforcements to the floors
9) Optimize horizontally: join together any square plates of same color that can be replaced by a larger plate
10) Optimize vertically: replace any sequences of vertically stacked plates of same color with bricks
11) Optimize chunks: replace any sequences of 1x1 bricks by larger bricks
12) Repeat until no more optimizations remain
13) Export as .LDR that should be readable by the BrickLink Studio

====

NOTES:
* Sector flags: 1=No ceiling (sky); 128=No floor (pit); 1024=No walls (horizon)
* Obtaining a PIL.Image from a BM with a given palette:
  img = bmtool.convbm('resource\\if3.bm', 'resource\\secbase.pal')

====

* by fish (oton.ribic@gmail.com), 2025
* using BM library from dftools by Nicholas Jankowski

====

To do v1.1:
X consider the "no wall" flag of sectors
X consider the "no floor" flag of sectors
X handle areas where the specified BM is not available

'''

import sys
import os
import bmtool
import FreeSimpleGUI as sg

DEVMODE = False  # To be enabled during development

# Some constants, defaults, etc. which are unnecessary in function arguments
SWNAME = 'Brix Madine 1.1'
TILINGSTEPS = 0.02  # Step of a line when tracking tiles applicable for a line (in integer units)
PXPERDFU = 8  # How many pixels per DF unit, this is fixed
LEGOPLATEHEIGHT = 0.4  # How many studs is a LEGO plate high
REDUCTION = 0.8  # Multiplier of iterator steps to be fully sure that there will be no misjumps due to floats
FALLBACKBM = 'DEFAULT.BM'  # Default BM to be used if the specified one was not found
ABOUT = '''Use this program to convert .LEV Dark Forces files (along with their resources) to LEGO models that can be opened in programs such as BrickLink Studio, MLCAD, LDraw Viewer, etc. With the default scaling of 2 Dark Forces units per stud horizontally and 1.8 vertically, the resulting levels will be approximately in the LEGO minifig scale. Make sure that all BM's (from Vanilla Dark Forces or custom ones) are available in the resources directory you specify, or in the folder where the .LEV file is (which is searched secondarily if a BM is not found in the resources folder).

Keep in mind that the conversion itself (i.e. generating a LEGO file from .LEV) may take several minutes, and loading it in the chosen app comparably long.

Feel free to edit the dfmap.colors and dfmap.parts to edit the colors that the program will use for converting colors to LEGO colors, and to add bricks that the resulting level can be built with, even though defaults should work fine in most cases. If no color conversion is done, levels will be prettier but use colors which don't exist in LEGO. Color conversion may still result in some parts which don't really exist - i.e. not all bricks are available in all LEGO colors.

This app is freeware, no warranty nor any promise whatsoever. It uses dftools by Nicholas Jankowski.

Visit www.df-21.net and join the Dark Forces community! And play with LEGO!

-fish (Oton Ribic), oton.ribic@gmail.com, 2025'''

# Globals needed to avoid huge copies between functions
palette = []


# HELPERS

def _colorparse(file):
    '''Get a BrickLink-style TSV file and parse color ID's, names and RGB's from it.
    Default one included (dfmap.colors)
    '''
    inf = open(file)
    inf = [e.strip(' \n') for e in inf.readlines()[1:]]
    inf = [e for e in inf if e]
    # Got all file contents, now fill them in the collector
    palette = []
    for line in inf:
        line = line.split(' ')
        id = int(line[0])
        # Get RGB
        hexc = line[1]
        r, g, b = hexc[0:2], hexc[2:4], hexc[4:6]
        r = int(r, 16)
        g = int(g, 16)
        b = int(b, 16)
        # Add all to the collector
        palette.append((id, line[1], (r, g, b)))
        # in the form (ID, name, (r,g,b) )
    return palette


def _partparse(file):
    '''Get a CSV with width, length, height and filename of each part that could be used to assemble the level.
    Return as a [ (X[stud], Y[stud], Z[platehieight], filename), ... ] list.
    '''
    # Initial load
    inf = open(file)
    inf = [e.strip(' \n') for e in inf.readlines() if not e.startswith('#')]
    inf = [e.split(',') for e in inf]
    # Create collector
    parts = []
    # Process each
    for x, y, z, dat in inf:
        x = int(x)
        y = int(y)
        z = int(z)
        dat = dat.strip('"\' ')
        # Add to collector
        parts.append((x, y, z, dat))
        # Add its 90 deg rotated version to collector if applicable
        if x != y:
            parts.append((y, x, z, dat))
    # Sort to have bricks, then plates, then 1x1
    parts.sort(key=lambda k: k[0], reverse=True)
    parts.sort(key=lambda k: k[1], reverse=True)
    parts.sort(key=lambda k: k[2], reverse=True)
    return parts


def _polygonarea(crd):
    'Get a polygon area from a list of coordinates'
    sumlist = [(crd[c][0] * crd[c + 1][1] - crd[c][1] * crd[c + 1][0]) for c in range(len(crd) - 1)]
    sumlist.append(crd[-1][0] * crd[0][1] - crd[-1][1] * crd[0][0])
    return sum(sumlist) / 2


def _levparse(
        mapfile,  # .LEV file
        resfolder,  # where BM's or BMP's are
):
    '''Load and parse a .LEV to get all geometry and textures.
    Returns a list of sectors:
    [ [walls], flrtex, flrx, flry, ceiltex, ceilx, ceily, flralt, ceilalt, opensky]
    walls=[ wall, wall, wall, ...]
    wall=[xy start, xy end, midtx, midx, midy, toptx, topx, topy, bottx, botx, boty, adjoin]
    '''
    # Load & normalize
    inf = open(mapfile, 'r').readlines()
    inf = [e.strip(' \n\t') for e in inf]
    inf = [e for e in inf if e and not e.startswith('#')]

    # Set up collectors
    textures = []  # List of .BM's
    sectors = []  # Containing sectors, each being their own list
    # Local aggregator of sectors: walls, floor tx, ox,y, ceiling tx, ox,y, flooralt, ceilalt, opensky, openflr, nowall
    locsector = [[], None, None, None, None, None, None, None, None, None, None, None]  # Reset it

    # Process line-by-line
    for line in inf:
        # Texture?
        if line.startswith('TEXTURE:'):
            line = line[8:]
            line = line.partition('#')[0]
            line = line.strip(' ')
            line = resfolder + line
            textures.append(line)
        # Sector?
        if line.startswith('SECTOR'):
            # Firstly append the previous one if valid
            if locsector[0]:
                sectors.append(locsector)
            locsector = [[], None, None, None, None, None, None, None, None, None, None, None]  # Reset it
            wall = [None, None, None, None, None, None, None, None, None, None, None, None]
        # Floor or ceiling alt?
        if line.startswith('FLOOR ALTITUDE'):
            locsector[7] = -float(line[14:])
        if line.startswith('CEILING ALTITUDE'):
            locsector[8] = -float(line[16:])
        # Floor or ceiling texture?
        if line.startswith('FLOOR TEXTURE'):
            ln = line[13:].strip(' ')
            ln = [e for e in ln.split(' ') if e]
            locsector[1] = textures[int(ln[0])]
            locsector[2] = float(ln[1])
            locsector[3] = float(ln[2])
        if line.startswith('CEILING TEXTURE'):
            ln = line[15:].strip(' ')
            ln = [e for e in ln.split(' ') if e]
            locsector[4] = textures[int(ln[0])]
            locsector[5] = float(ln[1])
            locsector[6] = float(ln[2])
        # Various flags
        if line.startswith('FLAGS'):
            flagvalue = int(line[5:].strip(' ').partition(' ')[0])
            # No sky?
            locsector[9] = bool(flagvalue % 2)
            # No floor?
            locsector[10] = bool(flagvalue % 256 // 128)
            # No walls?
            locsector[11] = bool(flagvalue % 2048 // 1024)
        # Vertices list?
        if line.startswith('VERTICES'):
            locvertex = []  # Clear vertex list
        # Vertex?
        if line.startswith('X:'):
            lx = float(line[2:].strip(' ').partition(' ')[0])
            ly = float(line[line.index('Z:') + 2:].strip(' ').partition(' ')[0])
            locvertex.append((lx, ly,))
        # Wall?
        if line.startswith('WALL '):
            # Filler: start, stop, midtex, ox,oy, toptex, ox,oy, bottex, ox,oy, adjoin
            wall = [None, None, None, None, None, None, None, None, None, None, None, None]
            # Vertices first
            v1 = int(line[line.index('LEFT:') + 5:].strip(' ').partition(' ')[0])
            v2 = int(line[line.index('RIGHT:') + 6:].strip(' ').partition(' ')[0])
            wall[0] = locvertex[v1]
            wall[1] = locvertex[v2]
            # Textures
            mid = line.index('MID:')
            top = line.index('TOP:')
            bot = line.index('BOT:')
            sign = line.index('SIGN:')
            # Mid
            cmid = line[mid + 4:top].strip(' ')
            cmid = [e for e in cmid.split(' ') if e]
            wall[2] = textures[int(cmid[0])]
            wall[3] = float(cmid[1])
            wall[4] = float(cmid[2])
            # Top
            ctop = line[top + 4:bot].strip(' ')
            ctop = [e for e in ctop.split(' ') if e]
            wall[5] = textures[int(ctop[0])]
            wall[6] = float(ctop[1])
            wall[7] = float(ctop[2])
            # Bot
            cbot = line[bot + 4:sign].strip(' ')
            cbot = [e for e in cbot.split(' ') if e]
            wall[8] = textures[int(cbot[0])]
            wall[9] = float(cbot[1])
            wall[10] = float(cbot[2])
            # Adjoin
            cad = int(line[line.index('ADJOIN:') + 7:].strip().partition(' ')[0])
            wall[11] = cad
            # Finally add to sector
            locsector[0].append(wall)
    if locsector[0]:
        sectors.append(locsector)

    return sectors


def _levrefine(lev):
    '''Take the geometry created by _levparse and refine it to the next step: convert the entire level to a list of
    sectors along with their geometry and textures, and the list of individual walls. There is no change for regular
    sector walls, but there are calculations and logics needed for the adjoined walls, to calculate the overhangs,
    steps, directions, etc. Ultimately this simplified version just lists each wall with its start and end altitude,
    and only one texture, with it either being anchored on the top or on the bottom. (I.e. an adjoined wall with both
    floor and ceiling differences will result in two walls, and the same-altitude sectors will adjoin to no walls.
    Walls still need to be "attached" to sectors rather than independent, because of their possible priority ordering
    later.
    Output format:
    [sector, sector, sector, ...]
    sector:
    [walls, floor texture, floorofx, floorofy, ceiling texture, ceilofx,
     ceilofy, flr alt, ceil alt, opensky?, openflr?, nowalls?, area, originalwalls]
    walls:
    [wall, wall, wall, ...]
    wall:
    [start xy, end xy, bottom alt, top alt, texture, tx offset x, tx offset y, anchored from its top? ]
    originalwalls:
    [owall, owall, owall, ...]
    owall:
    ( ((x1,y1),(x2,y2)), ((x1,y1),(x2,y2)), ...]
    '''
    # The replacements can mostly be done in place, on the 'lev' itself
    # Iterate over sectors first
    for scid, sector in enumerate(lev):
        # Calculate sector area
        coords = [e[0] for e in sector[0]]
        sarea = abs(_polygonarea(coords))
        swalls = []  # Local collector of walls in this sector
        origwalls = []  # Local collector of 'pure', original walls
        for wall in sector[0]:
            # Add to original walls list
            origwalls.append((wall[0], wall[1]))
            # Now check the 'wall' for adjoins
            # Typically it will NOT be adjoined:
            if wall[-1] == -1:
                # Apply only the mid texture, anchored from the bottom
                newwall = [wall[0], wall[1], sector[7], sector[8], wall[2], wall[3], wall[4], False]
                swalls.append(newwall)
            else:
                # The wall is adjoined, do some more checks
                adsect = wall[-1]  # Sector ID it adjoins to
                # Adjoined sector's floor and ceiling altitudes
                adflr = lev[adsect][7]
                adceil = lev[adsect][8]
                # Is there a step on our side (is the adjoined sector's floor higher)?
                if adflr > sector[7]:
                    # Yes, there is a step on our side
                    newwall = [wall[0], wall[1], sector[7], adflr, wall[8], wall[9], wall[10], False]
                    swalls.append(newwall)
                # Likewise, is there an overhang (is the adjoined sector's ceiling lower)?
                if adceil < sector[8]:
                    # Yes, there is an overhang on our side
                    newwall = [wall[0], wall[1], adceil, sector[8], wall[5], wall[6], wall[7], True]
                    swalls.append(newwall)
        # Now added all walls to 'swalls'
        # Replace it in the 'lev'
        lev[scid][0] = swalls
        # Add the sector area at its end
        lev[scid].append(sarea)
        # Add pure walls at its end
        lev[scid].append(origwalls)

    # Done all the replacements, return the map
    return lev


def _gettiles(p1, p2):
    '''Get all the tiles covered by the line between p1 (x1,y1) and p2 (x2,y2).
    '''
    # Determine step
    xs = p2[0] - p1[0]
    ys = p2[1] - p1[1]
    length = ((xs**2) + (ys**2))**0.5
    if length < 0.01:
        # Is the length zero? If yes, just consider that single column
        return {(int(p1[0]), int(p1[1])), }
    divisor = length / TILINGSTEPS
    if divisor < 1: divisor = 1  # Avoid microsteps
    stepx = xs / divisor
    stepy = ys / divisor
    # Create a list of tracked points
    points = [(p1[0] + stepx * e, p1[1] + stepy * e) for e in range(int(divisor))]
    # Final pair of points (for possible rounding/stepping errors!)
    points.append(p2)
    # Condense to integers
    points = {(int(x), int(y)) for (x, y) in points}
    # It is a set, because essentially the order of tiles is irrelevant
    return points


def _matchcolor(rgb,  # (r,g,b) format
                direct=False,  # Just return the actual color in hex (special LDraw trick)
                ):
    '''Get the LEGO color which is nearest to the supplied RGB triplet
    '''
    if direct:
        # Forget about matching, return the raw HEX
        return '0x2{0:02x}{1:02x}{2:02x}'.format(*rgb)
    global palette
    best = 768  # Maximum miss possible - which will never happen
    # Iterate over available LEGO colors
    for colorid, colorname, crgb in palette:
        # Get the delta from the reference RGB
        diffs = sum([abs(rgb[e] - crgb[e]) for e in (0, 1, 2)])
        # Now check these delta sums
        if diffs == 0:
            # Found exact match, abort the search
            return colorid
        # Better match than the best one so far?
        if diffs < best:
            best = diffs
            bestcol = colorid
    # Found the best one, having iterated over all
    return bestcol


def _intersect(A, B, C, D):
    '''Get two line segments: AB and CD, each a pair of (x,y) points, and answer whether they intersect'''
    def ccw(A, B, C):
        return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])
    return ccw(A, C, D) != ccw(B, C, D) and ccw(A, B, C) != ccw(A, B, D)


def _tilepolygon(walls,  # List of walls
                 offsetx=0.5,  # Local X offset specifying at what point should tile eligibility be checked
                 offsety=-0.5,  # See above, for Y offset. x=y=0.5 means tile center, which is default
                 ):
    '''Get a list of walls (each wall being ((x1,y1),(x2,y2)) ), and find out which integer tiles are enclosed in that
    polygon. Assume it is always closed, though it may have negative "sub-polygons". Use the even-odd crossing
    method and return a set of all applicable tiles. By definition the algorithm is immune to the direction of the
    walls and gracefully survives occasional walls being disconnected.
    '''
    # Firstly get extents of the area
    xc = [e[0][0] for e in walls] + [e[1][0] for e in walls]
    yc = [e[0][1] for e in walls] + [e[1][1] for e in walls]
    minx = int(min(xc)) - 2
    miny = int(min(yc)) - 2
    maxx = int(max(xc)) + 3
    maxy = int(max(yc)) + 3
    # Prepare final tile collector
    tiles = []
    # Scan line after line over the area, counting intersections
    for column in range(minx, maxx):
        crosscounter = 0  # How many line crossings, reset each column
        # Now iterate the column from-top-downward
        for y in range(miny, maxy):
            # Calculate actual line (for offsets!)
            chlinea = (column + offsetx, y + offsety)
            chlineb = (column + offsetx, y + offsety + 1)
            # Check for any intersections with known walls
            for wa, wb in walls:
                # Check the wall vs line
                if _intersect(chlinea, chlineb, wa, wb):
                    crosscounter += 1
            # If the number of crosses is odd, the tile is valid
            if crosscounter % 2:
                tiles.append((column, y))
    # Done, return the list of tiles if any
    return tiles


def brickify(
        grid,  # Set of input "plates" in lego stud units (x,y,z,colorcode)
        parts,  # A list of available parts (x[stud],y[stud],z[plates],DAT),...
        pltheight=0.4,  # Height of a plate in studs
):
    '''Iterate through the entire grid and see, from larger to smaller, which parts can be used to represent grid
    points if they are neighbors. Take into account the list of available parts, as well as the fact that the
    non-square parts can be oriented in two directions.
    The fact that some plate height rounding will occur is a necessary compromise.
    Note that the inputs are all in studs (height is not in plates).
    Output:
    [ (partcx, partcy, partch, DATfile, colorcode, rotated90?), ... ]
    '''
    # Convert heights from studs to plates
    grid = [(x, y, round(h / pltheight), color) for x, y, h, color in grid]
    # Firstly calculate extents and normalize grid
    xs = [c[0] for c in grid]
    ys = [c[1] for c in grid]
    hs = [c[2] for c in grid]
    minx = min(xs)
    miny = min(ys)
    maxx = max(xs)
    maxy = max(ys)
    minh = min(hs)
    maxh = max(hs)
    # Build a normalized dictionary
    map = dict()
    for x, y, h, color in grid:
        map[(x, y, h)] = color
    # Collector of final 'brickified' parts
    assembly = []
    # Iterate over 1x1x1 plates in 'map' and check if they can be covered by 'parts' in that exact order,
    # part by part, starting with the largest. The last one will always be 1x1x1 which will cover everything
    counter = 0
    for partx, party, partz, partdat in parts:
        sg.one_line_progress_meter('Determining LEGO parts...', counter, len(parts), no_button=True, orientation='h')
        counter += 1
        # X,Y,Z of parts are the length, width and height
        for x, y, h in list(map.keys()):
            if (x, y, h) not in map.keys(): continue  # The grid element is already removed, ignore it
            ccolor = map[(x, y, h)]  # Color to be searched and matched
            # Now check if any parts originating from this point can be assembled from, extending
            # partx, party, partz studs or plates from x,y,h
            coords = []
            # Get coordinates for all aplicable positions for this part
            check = [[[coords.append((cx, cy, cz)) for cx in range(x, x + partx)] for cy in range(y, y + party)]
                     for cz in range(h, h + partz)]
            # Check their colors
            check = [map[c] if (c in map.keys()) else None for c in coords]
            # Check if they are equal to searched color
            check = [c == ccolor for c in check]
            # Check if all are true (if yes, then the part can be inserted indeed)
            if not all(check): continue  # Not, so search on
            # All colors are valid, so the corresponding part which covers 'coords' can be inserted here
            # Calculate its center
            pcx = x + partx / 2
            pcy = y + party / 2
            pch = h + partz / 2
            # Now, special exception if a 3-plate-high brick because of the ugly way it is constructed, which
            # has its height coordinate lower by one plate height
            if partz == 3: pch += 1
            # Is it flipped (Y>Z) or not?
            pflip = party > partx
            # Add the part to the overall list
            assembly.append((pcx, pcy, pch, partdat, ccolor, pflip))
            # Remove the replaced coordinates from the map, as "it's been taken care of"
            for x, y, h in coords:
                del map[(x, y, h)]
    # All parts and coordinates iterated over,
    sg.one_line_progress_meter_cancel()
    # return all results
    return assembly


def _formatoutput(parts, outfile):
    '''Get parts in the LDRAW-normalized format x,y,ht,DAT,color,rotated, and return a string object that can be
    written to the output file as a .LDR
    '''
    # Prepare header first in the collector
    out = [('0 Untitled\n0 Name: {0}\n0 Author: MILSGen\n0 Unofficial Model\n' +
            '0 ROTATION CENTER 0 0 0 1 "Custom"\n0 ROTATION CONFIG 0 0\n').format(outfile),]
    # Add all parts
    for x, y, h, dat, color, rotated in parts:
        # Coordinates
        locstr = '1 {4} {0} {2} {1} '.format(x, y, h, dat, color)
        # Transformations
        if rotated:
            locstr += '0 0 -1 0 1 0 1 0 0 '
        else:
            locstr += '1 0 0 0 1 0 0 0 1 '
        # DAT
        locstr += dat + '\n'
        # Add to collector
        out.append(locstr)
    # Add footer
    out.append('0\n')
    # Finally assemble the entire out together
    return ''.join(out)


# MAIN GENERATOR FUNCTION

def dfmap(
        mapfile,  # Actual .LEV with optional path
        palfile=None,  # Palette file (will be auto-found if omitted)
        colorfile='dfmap.colors',  # Containing part colors data
        partsfile='dfmap.parts',  # Containing part dimensions data
        resfolder=None,  # Folder with resources, mainly BM or BMP files used in the map
        outfile=None,  # Auto-generated if missing
        prioritylarge=True,  # Whether the larger sectors will "overpower" smaller ones
        xyscale=2,  # How many DFU units per LEGO stud in horizontal axes
        zscale=1.8,  # How many DFU units per LEGO stud in vertical axis (plate height=0.4 studs)
        rounding=3,  # Number of decimals to round the geometry to, to avoid float errors
        generateceilings=True,  # Whether to build ceilings of each sector (unless open sky)
        outputscale=(20, 20, -8),  # Output scale multiplier X,Y,height
        directrgb=False,  # Use direct RGB part colors instead of LEGO colors (warning! it will result in many parts)
):
    global palette
    # Check if map file exists
    if not os.path.isfile(mapfile):
        print('Missing map file:', mapfile)
        sys.exit(1)
    # Fill in missing input parameters
    # Palette file
    if not palfile:
        palfile = mapfile.rpartition('.')[0] + '.PAL'
        if not os.path.isfile(palfile):
            print('Could not locate PAL file:', palfile)
            sys.exit(1)
    # Resources folder
    if not resfolder:
        resfolder = mapfile.rpartition('/')[0] + '/'
    if not resfolder.endswith('/'):
        resfolder += '/'
    # Alternate resources folder (where the .LEV is)
    levfolder = mapfile.rpartition('/')[0] + '/'
    # Output file
    if not outfile:
        outfile = mapfile.rpartition('.')[0] + '.LDR'

    # Parse the .LEV to individual sectors and their wall lists
    lev = _levparse(mapfile, resfolder)

    # Calculate geometry to account for adjoined walls
    lev = _levrefine(lev)

    # Reorder the geometry if needed
    lev.sort(key=lambda e: e[10], reverse=(not prioritylarge))

    # Load colors
    palette = _colorparse(colorfile)

    # Load parts
    parts = _partparse(partsfile)

    # Prepare the collector of parts
    legoplates = []

    # First loop cycle, with sector walls
    for ctr, sector in enumerate(lev):
        sg.one_line_progress_meter('Calculating wall geometry...', ctr, len(lev), no_button=True, orientation='h')
        # Firstly check whether walls are even needed
        if sector[11]: continue  # Not because noWalls flag is True
        # Proceed to iterate over sector walls
        for wall in sector[0]:
            # Process that wall
            # Split to local parameters
            pa, pb, pbot, ptop, texture, offx, offy, topanchor = wall
            # Convert wall geometry from DFU to LEGO studs by reference scale and round to avoid float errors
            # Start and end points A,B
            a = round(pa[0] / xyscale, rounding), round(pa[1] / xyscale, rounding)
            b = round(pb[0] / xyscale, rounding), round(pb[1] / xyscale, rounding)
            # Wall bottom and top
            bot = round(pbot / zscale, rounding)
            top = round(ptop / zscale, rounding)
            # Knowing its stud geometry, find out the relevant tiles
            tiles = _gettiles(a, b)
            # Get the texture of this wall, needed during later loops
            if os.path.isfile(texture):
                teximg = bmtool.convbm(texture, palfile)
            else:
                # Perhaps in the level folder?
                alternate = texture.rpartition('/')[2]
                alternate = levfolder + alternate
                if os.path.isfile(alternate):
                    teximg = bmtool.convbm(alternate, palfile)
                else:
                    # Not there either, so fallback
                    teximg = bmtool.convbm(FALLBACKBM, palfile)
                    print('Warning: File', texture, 'not found, using fallback BM')
            # Process each tile
            for tilex, tiley in tiles:
                # Get the tile's distance from the wall start (necessary for texturing)
                tiledist = ((tilex - a[0])**2 + (tiley - a[1])**2)**.5
                # Convert the LEGO distance to the DFU distance
                pixeldist = tiledist * xyscale
                # Apply horizontal offset to it
                pixeldist += offx
                # Convert it to pixels
                pixeldist *= PXPERDFU
                # Normalize the value for the case of some extremes
                if pixeldist < 0: pixeldist = 0
                # Now we know our tile at (tilex,tiley) is 'pixeldist' pixels away from the wall start point
                # Get the relevant column (X position) in the texture based on repeats
                texposx = int(pixeldist) % teximg.size[0]
                # Knowing the tile position and texture pixel position it applies to, iterate over height
                # Iterate height above each such tile (in LEGO units) from bot to top
                curheight = bot  # Starting from bottom of the wall upward
                while curheight <= top:
                    # Process the curheight (in LEGO unit)
                    # This plate is at tilex,tiley,curheight
                    # Get vertical position in the wall
                    if topanchor:
                        # Textures progressing downward from the top
                        # Distance from the top (equals zero when top reached)
                        vertpos = top - curheight
                        # Convert to DFU
                        vertpos *= zscale
                        # Apply vertical offset to it
                        vertpos += offy
                        # Convert it to pixels
                        vertpos *= PXPERDFU
                        # This is now the pixel position, get the true Y based on texture
                        texposy = int(vertpos) % teximg.size[1]
                    else:
                        # Textures progressing upward from the bottom
                        # Distance from the bottom (starts at zero) and rises
                        vertpos = bot - curheight
                        # Convert to DFU
                        vertpos *= zscale
                        # Apply vertical offset to it
                        vertpos += offy
                        # Convert to pixels
                        vertpos *= PXPERDFU
                        # This is now the pixel position, get the true Y based on texture
                        texposy = int(vertpos) % teximg.size[1]
                    # We got the texposx,texposy in the selected texture to fetch the color from
                    targcol = teximg.getpixel((texposx, texposy))[0:3]  # To make sure only RGB is here, not RGBA
                    # Find the nearest LEGO color to the given one
                    legocol = _matchcolor(targcol, directrgb)
                    # Now place the 1x1 plate at position tilex,tiley,curheight with 'legocol' LEGO color ID
                    legoplates.append((tilex, tiley, curheight, legocol))
                    # Advance to the next heightupward in the next round
                    curheight += (LEGOPLATEHEIGHT * REDUCTION)
                    # No rounding at this point to avoid "laddering" and skipping some plate heights
    sg.one_line_progress_meter_cancel()

    # All walls done, now the second cycle iterates over floors and ceilings
    for ctr, sector in enumerate(lev):
        sg.one_line_progress_meter('Calculating floors & ceilings...', ctr, len(lev), no_button=True, orientation='h')
        # Split to innerlocal variables
        walls, flrtx, flrox, flroy, ceiltx, ceilox, ceiloy, flralt, ceilalt, \
            opensky, openflr, nowalls, area, origwalls = sector
        # Convert DFU in walls to LEGO studs
        for id in range(len(origwalls)):
            s, e = origwalls[id]
            s = (round(s[0] / xyscale, rounding), round(s[1] / xyscale, rounding))
            e = (round(e[0] / xyscale, rounding), round(e[1] / xyscale, rounding))
            origwalls[id] = (s, e)
        # Get tiles of the sector area (only X,Y whereas height is calculated later)
        stiles = _tilepolygon(origwalls)
        # Heights calculation
        lflr = round(flralt / zscale, rounding)
        lceil = round(ceilalt / zscale, rounding)
        # Process each tile's floor, and possibly its ceiling too
        for tilex, tiley in stiles:
            # Get its DFU position
            dfx = round(tilex * xyscale, rounding)
            dfy = round(tiley * xyscale, rounding)
            # Floors and ceilings have to be dealt with separately because of different textures, offsets, etc.

            # FlOORS
            if not openflr:
                # Get offsets
                fdfx = dfx + flrox
                fdfy = dfy + flroy
                # With offset applied, get the pixel positions
                fpxx = fdfx * PXPERDFU
                fpxy = fdfy * PXPERDFU
                if os.path.isfile(flrtx):
                    teximg = bmtool.convbm(flrtx, palfile)
                else:
                    # Perhaps in the level folder?
                    alternate = flrtx.rpartition('/')[2]
                    alternate = levfolder + alternate
                    if os.path.isfile(alternate):
                        teximg = bmtool.convbm(alternate, palfile)
                    else:
                        # Not there either, so fallback
                        teximg = bmtool.convbm(FALLBACKBM, palfile)
                        print('Warning: File', flrtx, 'not found, using fallback BM')
                fpxx = int(fpxx % teximg.size[0])
                fpxy = int(fpxy % teximg.size[1])
                # Knowing pixel position, check the color in the image
                targcol = teximg.getpixel((fpxx, fpxy))[0:3]  # To make sure it is RGB, not RGBA
                # Get nearest LEGO color
                legocol = _matchcolor(targcol, directrgb)
                # Add the tile to the corresponding layer
                legoplates.append((tilex, tiley, lflr, legocol))

            # CEILINGS (if requested and applicable)
            if generateceilings and not opensky:
                # Get offsets
                cdfx = dfx + ceilox
                cdfy = dfy + ceiloy
                # Get pixel positions now that offsets have been applied
                cpxx = cdfx * PXPERDFU
                cpxy = cdfy * PXPERDFU
                if os.path.isfile(ceiltx):
                    teximg = bmtool.convbm(ceiltx, palfile)
                else:
                    # Perhaps in the level folder?
                    alternate = ceiltx.rpartition('/')[2]
                    alternate = levfolder + alternate
                    if os.path.isfile(alternate):
                        teximg = bmtool.convbm(alternate, palfile)
                    else:
                        # Not there either, so fallback
                        teximg = bmtool.convbm(FALLBACKBM, palfile)
                        print('Warning: File', ceiltx, 'not found, using fallback BM')
                cpxx = int(cpxx % teximg.size[0])
                cpxy = int(cpxy % teximg.size[1])
                # Knowing pixel position, check the color in the image
                targcol = teximg.getpixel((cpxx, cpxy))[0:3]  # To make sure it is RGB, not RGBA
                # Get nearest LEGO color
                legocol = _matchcolor(targcol, directrgb)
                # Add the tile to the corresponding layer
                legoplates.append((tilex, tiley, lceil, legocol))
    sg.one_line_progress_meter_cancel()

    # Walls, floors and optional ceilings' plates added to 'legoplates' list which essentially means 1x1x1 plates of
    # various colors. These, now, need to be brickified to consist of larger parts where possible, via simple scanning
    # and color comparison
    partset = brickify(legoplates, parts, LEGOPLATEHEIGHT)

    # Now that all the bricks have been defined, convert to output scale
    scaledpartset = []
    for x, y, h, dat, color, rotated in partset:
        scaledpartset.append((round(x * outputscale[0]), round(y * outputscale[1]), round(h * outputscale[2]),
                              dat, color, rotated))

    # Prepare the content for the file output
    outcontent = _formatoutput(scaledpartset, outfile.rpartition('/')[2])

    # Save the file
    outf = open(outfile, 'w')
    outf.write(outcontent)
    outf.close()

    # Notify
    numparts = len(partset)
    quickmsg = 'Done, exported file ' + outfile + ' with ' + str(numparts) + ' parts'
    sg.popup_quick_message(quickmsg, auto_close_duration=4)


### Main run ###
################

if __name__ == '__main__':

    # Debug/dev mode selftest
    if DEVMODE:
        mf = r'C:/Users/otonr/AppVault/DOS/GAME/DF/vanilla/gromas.LEV'
        # mf = r'C:/Users/otonr/AppVault/DOS/GAME/DF/wdfuse/ats2/secbase.LEV'
        # mf = r'C:/Users/otonr/AppVault/DOS/GAME/DF/wdfuse310/nraid133/secbase.lev'
        # mf = r'C:/Users/otonr/AppVault/DOS/GAME/DF/wdfuse310/capture/secbase.lev'
        # mf = r'C:/Users/otonr/AppVault/DOS/GAME/DF/wdfuse310/1room7/secbase.lev'
        dfmap(mapfile=mf,
              resfolder=r'C:/Users/otonr/AppVault/DOS/GAME/DF/vanilla',
              outfile='c:/users/otonr/desktop/ats2.ldr',
              generateceilings=False,
              directrgb=True,
              xyscale=1,  # 2 default
              zscale=0.8,  # 1.8 default
              )
        sys.exit(0)  # End selftest

    # No development mode; standard run
    print(SWNAME)
    # Initialize visual
    sg.theme('DarkGrey11')
    layout = [[sg.Text('Input .LEV file:'), sg.Input(key='lev'),
               sg.FileBrowse('Browse', file_types=(('Dark Forces Levels', '*.LEV'),))],
              [sg.Text('Palette .PAL file (auto-detected if left blank):'), sg.Input(key='pal'),
               sg.FileBrowse('Browse', file_types=(('Dark Forces Palettes', '*.PAL'),))],
              [sg.Text('Output .LDR file (auto-generated if left blank):'), sg.Input('brixmadine.ldr', key='ldr')],
              [sg.Text('Additional resources (e.g. BM files) directory:'),
               sg.Input(key='res'), sg.FolderBrowse('Browse')],
              [sg.Text('')],
              [sg.Text('X and Y scale (how many DF units per Lego stud):'),
               sg.Input('2.0', key='xyscale')],
              [sg.Text('Height scale (how many DF units per Lego stud)'),
               sg.Input('1.8', key='zscale')],
              [sg.Text('')],
              [sg.Checkbox('Generate ceilings', key='ceilings'),
               sg.Checkbox('Use exact RGB part colors', key='directcol', default=1)],
              [sg.Button('Generate'), sg.Button('About'), sg.Button('Quit'),
               sg.Text('Use the "/" path separators (forward slash rather than backslash)'),],
              ]
    window = sg.Window(SWNAME, layout, icon='brixmadine.ico')

    # Main loop
    while True:
        action, keys = window.read()
        # Exit
        if action == 'Quit' or action == 'Esc' or action == 'Escape' or action == sg.WIN_CLOSED:
            break

        # About
        if action == 'About':
            sg.popup(ABOUT, title='About Brix Madine')

        # Generate
        if action == 'Generate':
            # Prepare parameters for the main function
            lev = keys['lev']
            pal = keys['pal']
            ldr = keys['ldr']
            res = keys['res']
            xyscale = float(keys['xyscale'])
            zscale = float(keys['zscale'])
            ceilings = keys['ceilings']
            directcol = keys['directcol']
            dfmap(
                mapfile=lev,
                palfile=pal,
                resfolder=res,
                outfile=ldr,
                xyscale=xyscale,
                zscale=zscale,
                generateceilings=ceilings,
                directrgb=directcol,
            )
