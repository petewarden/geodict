# Geodict
# Copyright (C) 2010 Pete Warden <pete@petewarden.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import MySQLdb, string
import geodict_config

# The main entry point. This function takes an unstructured text string and returns a list of all the
# fragments it could identify as locations, together with lat/lon positions
def find_locations_in_text(text):

    try:
        cursor = get_database_connection()
    except:
        print "Database connection failed. Have you set up geodict_config.py with your credentials?"
        return None

    current_index = len(text)-1
    result = []
    
    # This loop goes through the text string in *reverse* order. Since locations in English are typically
    # described with the broadest category last, preceded by more and more specific designations towards
    # the beginning, it simplifies things to walk the string in that direction too
    while current_index>=0:
    
        # This holds the results of the match function for the final element of the sequence. This lets us
        # optimize out repeated calls to see if the end of the current string is a country for example
        match_cache = {}
    
        # These 'token sequences' describe patterns of discrete location elements that we'll look for.
        for token_sequence in token_sequences:
            
            # The sequences are specified in the order they'll occur in the text, but since we're walking
            # backwards we need to reverse them and go through the sequence in that order too
            token_sequence = token_sequence[::-1]
    
            # Now go through the sequence and see if we can match up all the tokens in it with parts of
            # the string
            token_result = None
            token_index = current_index
            for token_position, token_name in enumerate(token_sequence):
            
                # The token definition describes how to recognize part of a string as a match. Typical
                # tokens include country, city and region names
                token_definition = token_definitions[token_name]  
                match_function = token_definition['match_function']
                
                # This logic optimizes out repeated calls to the same match function
                if token_position == 0 and token_name in match_cache:
                    token_result = match_cache[token_name]
                else:
                    # The meat of the algorithm, checks the ending of the current string against the
                    # token testing function, eg seeing if it matches a country name
                    token_result = match_function(cursor, text, token_index, token_result)
                    if token_position == 0:
                        match_cache[token_name] = token_result
                
                if token_result is None:
                    # The string doesn't match this token, so the sequence as a whole isn't a match
                    break
                else:
                    # The current token did match, so move backwards through the string to the start of
                    # the matched portion, and see if the preceding words match the next required token
                    token_index = token_result['found_tokens'][0]['start_index']-1
            
            # We got through the whole sequence and all the tokens match, so we have a winner!
            if token_result is not None:
                break
            
        if token_result is None:
            # None of the sequences matched, so back up a word and start over again
            ignored_word, current_index, end_skipped = pull_word_from_end(text, current_index)
        else:
            # We found a matching sequence, so add the information to the result
            result.append(token_result)
            found_tokens = token_result['found_tokens']
            current_index = found_tokens[0]['start_index']-1
    
    # Reverse the result so it's in the order that the locations occured in the text
    result = result[::-1]
    
    return result

# Functions that look at a small portion of the text, and try to identify any location identifiers

# Matches the current fragment against our database of countries
def is_country(cursor, text, text_starting_index, previous_result):

    current_word = ''
    current_index = text_starting_index
    pulled_word_count = 0
    found_row = None

    # Walk backwards through the current fragment, pulling out words and seeing if they match
    # the country names we know about
    while pulled_word_count < geodict_config.word_max:
        pulled_word, current_index, end_skipped = pull_word_from_end(text, current_index)
        pulled_word_count += 1
        if current_word == '':
            # This is the first time through, so the full word is just the one we pulled
            current_word = pulled_word
            # Make a note of the real end of the word, ignoring any trailing whitespace
            word_end_index = (text_starting_index-end_skipped)
        else:
            #
            current_word = pulled_word+' '+current_word

        # This happens if we've walked backwards all the way to the start of the string
        if current_word == '':
            return None

        # If the first letter of the name is lower case, then it can't be the start of a country
        # Somewhat arbitrary, but for my purposes it's better to miss some ambiguous ones like this
        # than to pull in erroneous words as countries (eg thinking the 'uk' in .co.uk is a country)
        if current_word[0:1].islower():
            continue

        # Now query the database to see if we have a country with this name
        select = 'SELECT * FROM countries WHERE country=%s LIMIT 1;'
        values = (current_word, )
#        print "Calling '"+(select % values)+"'"
        cursor.execute(select, values)
        found_row = cursor.fetchone()
        if found_row is not None:
            # We've found a valid country name
            break
        if current_index < 0:
            # We've walked back to the start of the string
            break
    
    if found_row is None:
        # We've walked backwards through the current words, and haven't found a good country match
        return None
    
    # Were there any tokens found already in the sequence? Unlikely with countries, but for
    # consistency's sake I'm leaving the logic in
    if previous_result is None:
        current_result = {
            'found_tokens': [],
        }
    else:
        current_result = previous_result
    
    found_dict = get_dict_from_row(cursor, found_row)
                                        
    country_code = found_dict['country_code']
    lat = found_dict['lat']
    lon = found_dict['lon']

    # Prepend all the information we've found out about this location to the start of the 'found_tokens'
    # array in the result
    current_result['found_tokens'].insert(0, {
        'type': 'COUNTRY',
        'code': country_code,
        'lat': lat,
        'lon': lon,
        'matched_string': current_word,
        'start_index': (current_index+1),
        'end_index': word_end_index 
    })
    
    return current_result

# Looks through our database of 2 million towns and cities around the world to locate any that match the
# words at the end of the current text fragment
def is_city(cursor, text, text_starting_index, previous_result):

    # If we're part of a sequence, then use any country or region information to narrow down our search
    country_code = None
    region_code = None
    if previous_result is not None:
        found_tokens = previous_result['found_tokens']
        for found_token in found_tokens:
            type = found_token['type']
            if type == 'COUNTRY':
                country_code = found_token['code']
            elif type == 'REGION':
                region_code = found_token['code']
    
    current_word = ''
    current_index = text_starting_index
    pulled_word_count = 0
    found_row = None
    while pulled_word_count < geodict_config.word_max:
        pulled_word, current_index, end_skipped = pull_word_from_end(text, current_index)
        pulled_word_count += 1
        
        if current_word == '':
            current_word = pulled_word
            word_end_index = (text_starting_index-end_skipped)
        else:
            current_word = pulled_word+' '+current_word

        if current_word == '':
            return None
        
        if current_word[0:1].islower():
            continue

        select = 'SELECT * FROM cities WHERE city=%s'
        if country_code is not None:
            select += ' AND country=%s'

        if region_code is not None:
            select += ' AND region_code=%s'
        
        # Unfortunately tuples are immutable, so I have to use this logic to set up the correct ones
        if country_code is None and region_code is None:
            values = (current_word, )
        elif country_code is not None and region_code is None:
            values = (current_word, country_code)
        elif country_code is None and region_code is not None:
            values = (current_word, region_code)
        else:
            values = (current_word, country_code, region_code)
        
        # There may be multiple cities with the same name, so pick the one with the largest population
        select += ' ORDER BY population DESC LIMIT 1;'
#        print "Calling '"+(select % values)+"'"
        cursor.execute(select, values)
        found_row = cursor.fetchone()
        if found_row is not None:
            break
        if current_index < 0:
            break
    
    if found_row is None:
        return None
    
    if previous_result is None:
        current_result = {
            'found_tokens': [],
        }
    else:
        current_result = previous_result
    
    found_dict = get_dict_from_row(cursor, found_row)
                                        
    lat = found_dict['lat']
    lon = found_dict['lon']
                
    current_result['found_tokens'].insert(0, {
        'type': 'CITY',
        'lat': lat,
        'lon': lon,
        'matched_string': current_word,
        'start_index': (current_index+1),
        'end_index': word_end_index 
    })
    
    return current_result

# This looks for sub-regions within countries. At the moment the only values in the database are for US states
def is_region(cursor, text, text_starting_index, previous_result):

    # Narrow down the search by country, if we already have it
    country_code = None
    if previous_result is not None:
        found_tokens = previous_result['found_tokens']
        for found_token in found_tokens:
            type = found_token['type']
            if type == 'COUNTRY':
                country_code = found_token['code']
    
    current_word = ''
    current_index = text_starting_index
    pulled_word_count = 0
    found_row = None
    while pulled_word_count < geodict_config.word_max:
        pulled_word, current_index, end_skipped = pull_word_from_end(text, current_index)
        pulled_word_count += 1
        if current_word == '':
            current_word = pulled_word
            word_end_index = (text_starting_index-end_skipped)
        else:
            current_word = pulled_word+' '+current_word

        if current_word == '':
            return None

        if current_word[0:1].islower():
            continue
        
        select = 'SELECT * FROM regions WHERE region=%s'
        if country_code is not None:
            select += ' AND country_code=%s'
        
        if country_code is None:
            values = (current_word, )
        else:
            values = (current_word, country_code)
        
        select += ' LIMIT 1;'
#        print "Calling '"+(select % values)+"'"
        cursor.execute(select, values)
        found_row = cursor.fetchone()
        if found_row is not None:
            break
        if current_index < 0:
            break
    
    if found_row is None:
        return None
    
    if previous_result is None:
        current_result = {
            'found_tokens': [],
        }
    else:
        current_result = previous_result
    
    found_dict = get_dict_from_row(cursor, found_row)

    region_code = found_dict['region_code']
    lat = found_dict['lat']
    lon = found_dict['lon']
                
    current_result['found_tokens'].insert(0, {
        'type': 'REGION',
        'code': region_code,
        'lat': lat,
        'lon': lon,
        'matched_string': current_word,
        'start_index': (current_index+1),
        'end_index': word_end_index 
    })
    
    return current_result

# A special case - used to look for 'at' or 'in' before a possible location word. This helps me be more certain
# that it really is a location in this context. Think 'the New York Times' vs 'in New York' - with the latter
# fragment we can be pretty sure it's talking about a location
def is_location_word(cursor, text, text_starting_index, previous_result):

    current_index = text_starting_index
    current_word, current_index, end_skipped = pull_word_from_end(text, current_index)
    word_end_index = (text_starting_index-end_skipped)
    if current_word == '':
        return None

    current_word = current_word.lower()
    
    if current_word not in geodict_config.location_words:
        return None

    return previous_result

# Utility functions

def get_database_connection():

    db=MySQLdb.connect(host=geodict_config.host,user=geodict_config.user,passwd=geodict_config.password,port=geodict_config.port)
    cursor=db.cursor()
    cursor.execute('USE '+geodict_config.database+';')

    return cursor

# Walks backwards through the text from the end, pulling out a single unbroken sequence of non-whitespace
# characters, trimming any whitespace off the end
def pull_word_from_end(text, index):

    whitespace = set(string.whitespace+"'\",.-/\n\r<>")

    found_word = ''
    current_index = index
    end_skipped = 0
    while current_index>=0:
        current_char = text[current_index]
        current_index -= 1
        
        char_as_set = set(current_char)
        
        if char_as_set.issubset(whitespace):
            if found_word is '':
                end_skipped += 1
                continue
            else:
                current_index += 1
                break
        
        found_word += current_char
    
    # reverse the result (since we're appending for efficiency's sake)
    found_word = found_word[::-1]
    
    return (found_word, current_index, end_skipped)

# Converts the result of a MySQL fetch into an associative dictionary, rather than a numerically indexed list
def get_dict_from_row(cursor, row):
    d = {}
    for idx,col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

# Types of locations we'll be looking for
token_definitions = {
    'COUNTRY': {
        'match_function': is_country
    },
    'CITY': {
        'match_function': is_city
    },
    'REGION': {
        'match_function': is_region
    },
    'LOCATION_WORD': {
        'match_function': is_location_word
    }
}

# Particular sequences of those location words that give us more confidence they're actually describing
# a place in the text, and aren't coincidental names (eg 'New York Times')
token_sequences = [
    [ 'CITY', 'COUNTRY' ],
    [ 'CITY', 'REGION' ],
    [ 'REGION', 'COUNTRY' ],
    [ 'COUNTRY' ],
    [ 'LOCATION_WORD', 'REGION' ], # Regions are too common as words to use without additional evidence
];
