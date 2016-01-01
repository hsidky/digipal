# -*- coding: utf-8 -*-
#from digipal_text.models import *
from digipal_text.models import TextContentXMLStatus, TextContent, TextContentXML
import re
from django.shortcuts import render
from django.http import HttpResponse, Http404
from django.db import transaction
from digipal import utils
from django.utils.datastructures import SortedDict
import json

import logging
dplog = logging.getLogger( 'digipal_debugger')

MAX_FRAGMENT_SIZE = 60000

def text_viewer_view(request, item_partid=0):
    
    from digipal.utils import is_model_visible
    if not is_model_visible('textcontentxml', request):
        raise Http404('Text view not enabled')
    
    from digipal.models import ItemPart
    context = {'item_partid': item_partid, 'item_part': ItemPart.objects.filter(id=item_partid).first()}
    
    # Define the content of content type and location type drop downs
    # on top of each panel
    context['dd_content_types'] = [
        {'key': 'transcription', 'label': 'Transcription', 'icon': 'align-left', 'attrs': [['data-class', 'Text']]},
        {'key': 'translation', 'label': 'Translation', 'icon': 'indent-left', 'attrs': [['data-class', 'Text']]},
        {'key': 'image', 'label': 'Image', 'icon': 'picture', 'attrs': [['data-class', 'Image']]},
    ]
    context['dd_location_types'] = [
        {'key': 'whole', 'label': 'Whole text', 'icon': 'book'},
        {'key': 'locus', 'label': 'Locus', 'icon': 'file'},
        {'key': 'entry', 'label': 'Entry', 'icon': 'entry'},
        {'key': 'section', 'label': 'Section', 'icon': 'section'},
        {'key': 'sync', 'label': 'Synchronise with', 'icon': 'magnet'},
    ]
    context['statuses'] = TextContentXMLStatus.objects.all().order_by('sort_order')
    
    update_viewer_context(context, request)
    
    return render(request, 'digipal_text/text_viewer.html', context)

def update_viewer_context(context, request):
    ''' To be overridden '''
    # TODO: design a better overriding model
    pass

def text_api_view(request, item_partid, content_type, location_type=u'default', location=''):
    
    format = request.REQUEST.get('format', 'html')
    if request.is_ajax(): format = 'json'
    
    from digipal.utils import is_model_visible
    if not is_model_visible('textcontentxml', request):
        raise Http404('Text view not enabled')
    max_size = MAX_FRAGMENT_SIZE if format == 'json' else None

    response = None
    
    # delegate to a custom function if it exists
    
    # Look up the content_type in the function name
    # e.g. content_type = image => text_api_view_image
    function = globals().get('text_api_view_' + content_type,  None)
    
    if function:
        response = function(request, item_partid, content_type, location_type, location, max_size=max_size)
    else:
        # Look up the content_type in the TextContentType table
        # e.g. content_type = translation or transcription, we assume it must be a TextContentXML
        from digipal_text.models import TextContentType
        content_type_record = TextContentType.objects.filter(slug=content_type).first()
        
        if content_type_record:
            response = text_api_view_text(request, item_partid, content_type, location_type, location, content_type_record, max_size=max_size)
    
    # we didn't find a custom function for this content type
    if response is None:
        response = {'status': 'error', 'message': 'Invalid Content Type (%s)' % content_type}

    if location_type == 'sync':
        # dummy response in case of syncing with another panel
        response['location'] = location
        response['location_type'] = location_type
        response['content'] = 'Syncing...'
        set_message(response, 'Syncing...', '')

    ret = None
    
    if format == 'json':
        ret = HttpResponse(json.dumps(response), mimetype='application/json')
    
    if format == 'html':
        context = {'response': response}
        context['display_classes'] = ' '.join((request.REQUEST.get('ds', '').split(',')))
        context['content_type_key'] = content_type
        ret = render(request, 'digipal_text/text_view.html', context)
        
    if format == 'pdf':
        context = {'response': response}
        context['display_classes'] = ' '.join((request.REQUEST.get('ds', '').split(',')))
        context['content_type_key'] = content_type
        from pdfview import render_to_pdf
        ret = render_to_pdf(request, 'digipal_text/text_view_pdf.html', context)

    if not ret:
        raise Exception('Unknown output format: "%s"' % format)

    return ret

def set_message(ret, message, status='error'):
    ret['message'] = message
    ret['status'] = status
    return ret

def text_api_view_text(request, item_partid, content_type, location_type, location, content_type_record, user=None, max_size=MAX_FRAGMENT_SIZE):
    ret = {}
    
    text_content_xml = None
    
    if not user and request:
        user = request.user
    
    #print 'content type %s' % content_type_record
    # 1. Fetch or Create the necessary DB records to hold this text
    from digipal.models import ItemPart
    item_part = ItemPart.objects.filter(id=item_partid).first()
    if item_part:
        #print 'item_part %s' % item_part
        # get or create the TextContent
        with transaction.atomic():
            text_content, created = TextContent.objects.get_or_create(item_part=item_part, type=content_type_record)
            # get or create the TextContentXML
            text_content_xml, created = TextContentXML.objects.get_or_create(text_content=text_content)
    
    if not text_content_xml:
        return set_message(ret, '%s not found' % content_type.capitalize())
    
    from digipal.utils import is_user_staff
    if not is_user_staff(user):
        if text_content_xml.is_private():
            if text_content_xml.content and len(text_content_xml.content) > 10:
                return set_message(ret, 'The %s will be made available at a later stage of the project' % content_type)
            else:
                return set_message(ret, '%s not found' % content_type.capitalize())

    record_content = text_content_xml.content or ''
    
    # 2. Load the list of possible location types and locations
    # return the locus of the entries
    if location_type == 'default' or utils.get_int_from_request_var(request, 'load_locations'):
        # whole
        ret['locations'] = SortedDict()
        
        # whole
        if max_size is not None and len(record_content) <= max_size and (content_type != 'codicology'):
            ret['locations']['whole'] = []
        
        # entry
        for ltype in ['entry', 'locus']:
            ret['locations'][ltype] = []
            if text_content_xml.content:
                for entry in re.findall(ur'(?:<span data-dpt="location" data-dpt-loctype="'+ltype+'">)([^<]+)', text_content_xml.content):
                    ret['locations'][ltype].append(entry)
            if not ret['locations'][ltype]: del ret['locations'][ltype]
    
    # resolve 'default' location request
    location_type, location = resolve_default_location(location_type, location, ret)
            
    # 3. Save the user fragment
    new_fragment = None
    if request:
        new_fragment = request.REQUEST.get('content', None)
    
    convert = utils.get_int_from_request_var(request, 'convert')
    save_copy = utils.get_int_from_request_var(request, 'save_copy')
    
    ret['content_status'] = text_content_xml.status.id
    
    extent = get_fragment_extent(record_content, location_type, location)
    ret['message'] = ''
    dry_run = 0
    if extent:
        # make sure we compare with None, as '' is a different case
        if new_fragment is not None:
            ret['message'] = 'Content saved'

            # insert user fragment
            len_previous_record_content = len(record_content)
            # TODO: UNCOMMENT!!!!!!!!!!!!!!!!!!!!!!!!!!
            if not dry_run:
                record_content = record_content[0:extent[0]]+new_fragment+record_content[extent[1]:]
    
            # we make a copy if the new content removes 10% of the content
            # this might be due to a bug in the UI
            if len(record_content) < (0.9 * len_previous_record_content):
                print 'Auto copy (smaller content)'
                text_content_xml.save_copy()
                
            # set the new content
            text_content_xml.content = record_content
            
            # auto-markup
            if convert:
                text_content_xml.convert()
                record_content = text_content_xml.content
                ret['message'] = 'Content converted and saved'
            
            # make a copy if user asked for it
            if save_copy:
                text_content_xml.save_copy()
                ret['message'] = 'Content backed up'
            
            # save the new content
            # TODO: UNCOMMENT!!!!!!!!!!!!!!!!!!!!!!!!!!
            if not dry_run:
                text_content_xml.save()
            
            # update the extent
            # note that extent can fail now if the user has remove the marker for the current location
            extent = get_fragment_extent(record_content, location_type, location)

    # 4. now the loading part (we do it in any case, even if saved first)
    if not extent:
        if ret['message']:
            ret['message'] += ' then '
        ret['message'] += 'location not found: %s %s' % (location_type, location)
        ret['status'] = 'error'
    else:
        if max_size is not None and (extent[1] - extent[0]) > max_size:
            if ret['message']:
                ret['message'] += ' then '
            ret['message'] += 'text too long (> %s bytes)' % (max_size)
            ret['status'] = 'error'
        else:
            ret['content'] = record_content[extent[0]:extent[1]]
            if new_fragment is None:
                ret['message'] = 'Content loaded'

    # we return the location of the returned fragment
    # this may not be the same as the requested location
    # e.g. if the requested location is 'default' we resolve it
    ret['location_type'] = location_type
    ret['location'] = location
        
    return ret

def resolve_default_location(location_type, location, response):
    if location_type == 'default':
        locations = response['locations']
        # grab the first available location
        for ltype in locations.keys():
            location_type = ltype
            location = ''
            if locations[ltype]:
                location = locations[ltype][0]
            break
    return location_type, location

def get_fragment_extent(content, location_type, location):
    ret = None
    
    print location_type, location
    
    content = content or ''
    
    if location_type == 'whole':
        ret = [0, len(content)]
    else:
        # extract the requested fragment
        # ASSUMES: root > p > span
        # ASSUME order of the attributes in the span (OK)
        # ... <p> </p> <p>...<span data-dpt="location" data-dpt-loctype="locus">1r</span>...</p> <p> </p> ... <p> <span data-dpt="location" data-dpt-loctype="locus">1r</span>
        span0 = content.find('<span data-dpt="location" data-dpt-loctype="'+location_type+'">'+location+'<')
        if span0 > -1:
            p0 = content.rfind('<p>', 0, span0)
            if p0 > -1:
                span1 = content.find('<span data-dpt="location" data-dpt-loctype="'+location_type+'">', span0 + 1)
                if span1 == -1:
                    ret = [p0, len(content)]
                else:
                    p1 = content.find('</p>', span1)
                    ret = [p0, p1+4]

    return ret

def text_api_view_image(request, item_partid, content_type, location_type, location, max_size=None):
    '''
        location = an identifier for the image. Relative to the item part
                    '#1000' => image with id = 1000
                    '1r'    => image with locus = 1r attached to selected item part
    '''
    ret = {}
    
    from digipal.models import Image
    
    request.visible_images = None
    
    visible_images = None
    def get_visible_images(item_partid, request, visible_images=None):
        if visible_images is None:
            ret = Image.objects.filter(item_part_id=item_partid)
            ret = Image.filter_permissions_from_request(ret, request, True)
            ret = Image.sort_query_set_by_locus(ret)
            visible_images = ret
        return visible_images
    
    # return the locus of the images under this item part
    # return #ID for images which have no locus
    if location_type == 'default' or utils.get_int_from_request_var(request, 'load_locations'):
        recs = Image.sort_query_set_by_locus(get_visible_images(item_partid, request, visible_images)).values_list('locus', 'id')
        ret['locations'] = SortedDict()
        if recs:
            ret['locations']['locus'] = ['%s' % (rec[0] or '#%s' % rec[1]) for rec in recs]

    # resolve 'default' location request
    location_type, location = resolve_default_location(location_type, location, ret)
    
    # find the image
    image = find_image(request, item_partid, location_type, location, get_visible_images, visible_images)
    
    # deal with writing annotations
    if request.method == 'POST':
        ret['newids'] = update_text_image_link(request, image)
    else:
        # image dimensions
        options = {}
        layout = request.REQUEST.get('layout', '')
        if layout == 'width':
            options['width'] = request.REQUEST.get('width', '100')
        
        # we return the location of the returned fragment
        # this may not be the same as the requested location
        # e.g. if the requested location is 'default' we resolve it
        ret['location_type'] = location_type
        ret['location'] = image.locus if image else location
        
        if image:
            #ret['content'] = iip_img(image, **options)
            ret['zoomify_url'] = image.zoomify()
            ret['width'] = image.width
            ret['height'] = image.height
            
            # add all the elements found on that page in the transcription
            ret['text_elements'] = get_text_elements_from_image(request, item_partid, 'transcription', location_type, location)
            
            # add all the non-graph annotations
            ret.update(get_annotations_from_image(image))
        
    return ret

def get_annotations_from_image(image):
    ret = {'annotations': []}
    
    from digipal.models import Annotation
    for annotation in Annotation.objects.filter(image=image, graph__isnull=True):
        info = {'geojson': annotation.get_geo_json_as_dict()}
        geojson = info['geojson']
        geojson['id'] = annotation.id
        # TODO: optimise the retrieval of the textannotations
        for textannotation in annotation.textannotations.all():
            geojson['properties'] = geojson.get('properties', None) or {}
            geojson['properties']['elementid'] = json.loads(textannotation.elementid or [])
        ret['annotations'].append(info)
        
    print ret
        
    return ret

def update_text_image_link(request, image):
    ret = {}
    
    print 'TEXT IMAGE LINK: image #%s' % image.id
    links = request.REQUEST.get('links', None)
    if links:
        ''' links = [
                        [
                            [["", "clause"], ["type", "address"]],
                            {"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[270,-1632],[270,-984],[3006,-984],[3006,-1632],[270,-1632]]]},"properties":null}
                        ],
                        [...]
                    ]
        '''
        for link in json.loads(links):
            print link
            attrs, geojson = link[0], link[1]
            clientid = (geojson.get('properties', {}) or {}).pop('clientid', '')
            serverid = geojson.pop('id', None)
            action = geojson.pop('action', 'update')
            
            # 1. find the annotation
            if not clientid and not serverid:
                raise Exception('Cannot find annotation, need either "id" or "clientid"')
            
            from digipal.models import Annotation
            filter = {}
            if serverid:
                filter['id'] = serverid
            else:
                filter['clientid'] = clientid
            annotation = Annotation.objects.filter(**filter).first()
            
            # 2. delete or create annotation
            if action == 'deleted':
                if annotation:
                    print 'delete annotation'
                    annotation.delete()
                    annotation = None
            else:
                # create annotation
                if not annotation:
                    if serverid:
                        raise Exception('Cannot find annotation by server id %s' % serverid)
                    print 'create annotation'
                    author = request.user
                    annotation = Annotation(clientid=clientid, image=image, author=author)
            
                # update annotation and link
                print 'update annotation'
                annotation.set_geo_json_from_dict(geojson)
                annotation.save()
                if not serverid:
                    ret[clientid] = annotation.id
                
                # update the text-annotation
                from digipal_text.models import TextAnnotation
                #text_annotation = TextAnnotation(annotation=annotation, element=json.dumps(attrs))
                # TODO: assume a single element per annotation for the moment
                text_annotation = TextAnnotation.objects.filter(annotation=annotation).first()
                if not attrs:
                    # delete it
                    if text_annotation:
                        print 'delete link'
                        text_annotation.delete()
                else:
                    if not text_annotation:
                        print 'create link'
                        text_annotation = TextAnnotation(annotation=annotation)
                    print 'update link'
                    text_annotation.elementid = json.dumps(attrs)
                    text_annotation.save()
        
    print ret
    
    return ret

def get_text_elements_from_image(request, item_partid, content_type, location_type, location):
    # returns the TRANSCRIPTION text for the requested IMAGE location
    # if not found, returns the whole text
    # eg.:  "text_elements": [[["", "clause"], ["type", "address"]], [["", "clause"], ["type", "disposition"]], [["", "clause"], ["type", "witnesses"]]]
    ret = []
    
    from digipal_text.models import TextContentType
    content_type_record = TextContentType.objects.filter(slug=content_type).first()
    
    # find the transcription for that image
    text_info = text_api_view_text(request, item_partid, content_type, location_type, location, content_type_record, user=None, max_size=MAX_FRAGMENT_SIZE)
    if text_info['status'] == 'error' and 'location not found' in text_info['message'].lower():
        # location not found, try the whole text
        text_info = text_api_view_text(request, item_partid, content_type, 'whole', '', content_type_record, user=None, max_size=MAX_FRAGMENT_SIZE)
    
    #print text_info

    # extract all the elements
    if text_info.get('status', '').lower() != 'error':
        content = text_info.get('content', '')
        if content:
            #for element in re.findall(ur'(?musi)((?:data-dpt-?([^=]*)="([^"]*)"[\s>]+)+)', content):
            for element in re.findall(ur'(?musi)data-dpt="[^>]+', content):
                # eg. parts: [(u'', u'clause'), (u'type', u'disposition')]
                parts = [attr for attr in re.findall(ur'(?musi)data-dpt-?([^=]*)="([^"]*)', element) if attr[0] not in ['cat']]
                # white list to filter the elements
                if parts[0][1] in ('clause', 'location'):
#                     element_info = []
#                     element_info[0] = ' '.join([part[0] for part in parts])
#                     element_info[1] = ' '.join([part[1] for part in parts])
#                     ret.append(element_info)
                    ret.append(parts)
        
    return ret

def find_image(request, item_partid, location_type, location, get_visible_images, visible_images):
    # return a Image object that matches the requested manuscript and location
    from digipal.models import Image

    image = None
    if location:
        imageid = re.sub(ur'^#(\d+)$', ur'\1', location)
        if imageid and imageid != location:
            # e.g. location = #100
            image = Image.objects.filter(id=imageid).first()
        else:
            # e.g. location = 13r
            image = Image.objects.filter(item_part_id=item_partid, locus=location).first()
            
            # e.g. location = 54b2 (entry number)
            # => convert to 54v
            if not image:
                locus = get_locus_from_location(location_type, location)
                if locus:
                    image = Image.objects.filter(item_part_id=item_partid, locus=locus).first()
    
    # Image not found, display the first one
    if not image or not image.is_full_res_for_user(request):
        image = get_visible_images(item_partid, request, visible_images).first()
        
    return image

def get_locus_from_location(location_type, location):
    ret = None
    
    # e.g. location = 54b2 (entry number)
    # => convert to 54v
    # TODO: check location_type
    # TODOL this is a customisation for EXON,
    # unlikely to be relevant for other projects
    parts = re.match('(\d+)([abrv]?)(\d*)', location)
    if parts:
        number = parts.group(1)
        side = 'r'
        if parts.group(2) and parts.group(2) == 'b': side = 'v'
        ret = number+side
        
    return ret

def text_api_view_search(request, item_partid, content_type, location_type, location, max_size=None):
    '''
        location = an identifier for the image. Relative to the item part
                    '#1000' => image with id = 1000
                    '1r'    => image with locus = 1r attached to selected item part
    '''
    ret = {}
    
    from digipal.templatetags.html_escape import iip_img
    from digipal.models import Image
    
    # return the locus of the images under this item part
    # return #ID for images which have no locus
    if location_type == 'default' or utils.get_int_from_request_var(request, 'load_locations'):
        ret['locations'] = SortedDict()
        ret['locations']['locus'] = ['%s' % (rec[0] or '#%s' % rec[1]) for rec in Image.sort_query_set_by_locus(Image.objects.filter(item_part_id=item_partid)).values_list('locus', 'id')]

    # resolve 'default' location request
    location_type, location = resolve_default_location(location_type, location, ret)
    
    query = request.REQUEST.get('query', '')
    entries = ''
    hit_count = 0
    if query:
        tcx = TextContentXML.objects.filter(text_content__type__slug='translation', text_content__item_part__id=item_partid).first()
        if tcx:
            for hit in get_entries_from_query(query):
                hit_count += 1
                entries += '<li><a data-location-type="entry" href="%s">%s</a><br/>%s</li>' % (hit['entryid'], hit['entryid'], hit['snippets'])
            
            #import re
            #match = re.search()
            #tcx.content
            #print tcx
        
    # e.g. if the requested location is 'default' we resolve it
    from django.utils.html import escape
    ret['location_type'] = location_type
    ret['location'] = location

    ret['content'] = ur'''<form class="text-search-form" method="GET">
        <p>Query: <input type="text" name="query" value="%s"/><input type="submit" name="s" value="Search"/></p>
        <p>%s entries</p>
        <ul>
            %s
        </ul>
    </form>''' % (escape(query), hit_count, entries)
    
    return ret

def get_entries_from_query(query):

    # run the query with Whoosh
    from django.conf import settings
    from whoosh.index import open_dir
    import os
    index = open_dir(os.path.join(settings.SEARCH_INDEX_PATH, 'faceted', 'textunits'))

    #from whoosh.qparser import QueryParser
    
    search_phrase = query
    
    # make the query
    # get the field=value query from the selected facet options
    field_queries = u''
    
    # add the search phrase
    if search_phrase or field_queries:
        qp = get_whoosh_parser(index)
        q = qp.parse(u'%s %s' % (search_phrase, field_queries))
    else:
        from whoosh.query.qcore import Every
        q = Every()
           
    ret = []
    
    with index.searcher() as s:
        # run the query
        #facets = self.get_whoosh_facets()

        hits = s.search(q, limit=1000000, sortedby='entryid_sortable')
        hits.fragmenter.charlimit = None
        
        # get highlights from the hits
        for hit in hits:
            ret.append({'entryid': hit['entryid'], 'snippets': hit.highlights('content', top=10)})
        #print '%s hits.' % len(hits)
                
    return ret

def get_whoosh_parser(index):
    from whoosh.qparser import MultifieldParser, GtLtPlugin
    
    # TODO: only active columns
    term_fields = ['content', 'entryid']
    parser = MultifieldParser(term_fields, index.schema)
    parser.add_plugin(GtLtPlugin)
    return parser

