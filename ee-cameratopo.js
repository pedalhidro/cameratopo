

/*
Camera Topográfica
Danilo Lessa Bernardineli
CC-BY-NC-SA 4.0
*/

var palettes = require('users/gena/packages:palettes');
var mapbiomas_palettes = require('users/mapbiomas/modules:Palettes.js');

var N_FIXED_LAYERS = 11;
var N_MUTABLE_LAYERS = 4;
var DEFAULT_ELE_COLOR_CYCLES = 1;
var DEFAULT_ELE_REL_MAX = 2.0;
var DEFAULT_ELE_REL_KERNEL = 1000;

function elevation_palette(color_cycles){
  var ele_palette = []
  for (var i = 0; i < color_cycles; i++){
    ele_palette = ele_palette.concat(palettes.cmocean.Phase[7])
  }
  return ele_palette
}

var ele_palette = elevation_palette(DEFAULT_ELE_COLOR_CYCLES);


Map.setOptions("HYBRID")
Map.setCenter(-46.6, -23.5, 10);

//var dsm_dataset = ee.ImageCollection('COPERNICUS/DEM/GLO30');

//var density_vis = {"max":200, "palette":["ffffe7","FFc869","ffac1d","e17735","f2552c","9f0c21"],"min":0};
var density_vis = {min: 0, max: 200, gamma: 2.0};
var slope_vis = {"min": 0, "max": 15};
var multiply_vis = {"min": 0, "max": 15, 'palette': ['ffffff', '000000']};
var elevation_vis = {"min": 700, "max": 1200, "palette": ele_palette};
var ele_rel_vis = {"min": -DEFAULT_ELE_REL_MAX, "max": DEFAULT_ELE_REL_MAX, "palette": palettes.cmocean.Curl[7]};
var ele_rel_rel_vis = {"min": 0.5, "max": 1.0, "gamma": 0.15};


var night_dataset = ee.ImageCollection('NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG')
                  .filter(ee.Filter.date('2020-09-01', '2024-09-01'));
var nighttime = night_dataset.select('avg_rad');
var nighttimeVis = {min: 0.0, max: 100.0, gamma: 3.0};



var aridity_index = ee.Image("projects/sat-io/open-datasets/global_ai/global_ai_yearly");
var image = aridity_index.multiply(0.0001);
// Define an SLD style of discrete intervals to apply to the image.
var aridity_index_style =
  '<RasterSymbolizer>' +
    '<ColorMap type="intervals">' +
      '<ColorMapEntry color="#ff0000" quantity="0.03" label="0-0.03"/>' +
      '<ColorMapEntry color="#ff8c00" quantity="0.21" label="0.03-0.2" />' +
      '<ColorMapEntry color="#f2ff00" quantity="0.51" label="0.2-0.51" />' +
      '<ColorMapEntry color="#dbfc03" quantity="0.65" label="0.5-0.65" />' +
      '<ColorMapEntry color="#00ffa6" quantity="1.00" label="0.66-1.00" />' +
      '<ColorMapEntry color="#00f2ff" quantity="1.50" label="1.00-1.50" />' +
      '<ColorMapEntry color="#0084ff" quantity="2.5" label=">1.50" />' +
    '</ColorMap>' +
  '</RasterSymbolizer>';
var cont_aridity_index_style =
  '<RasterSymbolizer>' +
    '<ColorMap>' +
      '<ColorMapEntry color="#ff0000" quantity="0.03" label="0-0.03"/>' +
      '<ColorMapEntry color="#ff8c00" quantity="0.21" label="0.03-0.2" />' +
      '<ColorMapEntry color="#f2ff00" quantity="0.51" label="0.2-0.51" />' +
      '<ColorMapEntry color="#dbfc03" quantity="0.65" label="0.5-0.65" />' +
      '<ColorMapEntry color="#00ffa6" quantity="1.00" label="0.66-1.00" />' +
      '<ColorMapEntry color="#00f2ff" quantity="1.50" label="1.00-1.50" />' +
      '<ColorMapEntry color="#0084ff" quantity="2.5" label=">1.50" />' +
    '</ColorMap>' +
  '</RasterSymbolizer>';
Map.addLayer(image.sldStyle(aridity_index_style),{},'Indice de Aridez (CGIAR-CIS, 1970-2020, Discreto)', false)
Map.addLayer(image.sldStyle(cont_aridity_index_style),{},'Indice de Aridez (CGIAR-CIS, 1970-2020, Continuo)', false)

var ano = 2023

var mb_palette = mapbiomas_palettes.get('classification9');
var vis = {'min': 0,'max': 69,'palette': mb_palette,'format': 'png'};
var imageVisParam2 = {"opacity":1,"min":103,"max":733,"gamma":1};
// Mapbiomas uso e cobertura
var colecao = ee.Image('projects/mapbiomas-public/assets/brazil/lulc/collection9/mapbiomas_collection90_integration_v1')
Map.addLayer(colecao.select('classification_'+ano), vis, 'MapBiomas col 9 - '+ ano, false);

var ano = 1985
var vis = {'min': 0,'max': 69,'palette': mb_palette,'format': 'png'};
var imageVisParam2 = {"opacity":1,"min":103,"max":733,"gamma":1};
// Mapbiomas uso e cobertura
var colecao = ee.Image('projects/mapbiomas-public/assets/brazil/lulc/collection9/mapbiomas_collection90_integration_v1')
Map.addLayer(colecao.select('classification_'+ano), vis, 'MapBiomas col 9 - '+ ano, false);

Map.addLayer(nighttime, nighttimeVis, 'Luzes Noturnas (NASA VIIRS, 2020-2024)', false);
Map.addLayer(claro_3g, {}, 'Cobertura Claro 3G (2024)', false);
Map.addLayer(eleicoes_cartograma.unmask(0), {}, 'Eleicoes 2022 - cartograma', false);
Map.addLayer(eleicoes_territorio, {}, 'Eleicoes 2022 - territorio', false);


var dataset = ee.ImageCollection("projects/sat-io/open-datasets/WORLDPOP/pop").filter(ee.Filter.stringContains('system:index', '_POP_' + 2025 + '_'));
var raster = dataset.select([0], ['population']).mosaic().unmask(0).reduceNeighborhood({
    reducer: ee.Reducer.mean(),
    kernel: ee.Kernel.gaussian(3000, 1000, "meters", true),
    //kernel: ee.Kernel.circle(5000, "meters", true),
    skipMasked: false
  });

Map.addLayer(raster, density_vis, 'Densidade Populacional (WorldPop)', false);
//var dataset = ee.ImageCollection("WorldPop/GP/100m/pop");
//var raster = dataset.select('population').mosaic().setDefaultProjection('EPSG:4326');
//var rasterMean = raster.reduceNeighborhood({reducer: ee.Reducer.mean(), kernel: ee.Kernel.circle(1000, 'meters'),});
//Map.addLayer(rasterMean, density_vis, 'Densidade Populacional (WorldPop)', false);

var fvLayer = ui.Map.FeatureViewLayer(
  'WWF/HydroSHEDS/v1/FreeFlowingRivers_FeatureView');

var visParams = {
  lineWidth: 1.5,
  color: {
    property: 'RIV_ORD',
    mode: 'linear',
    palette: palettes.misc.parula[7],
    min: 1,
    max: 9
  }
};

fvLayer.setVisParams(visParams);
fvLayer.setName('Curso estimado de rios livres (WWF HydroSHEDS)');

//var elevation = dsm_dataset.select('DEM');
var elevation = ee.ImageCollection('projects/sat-io/open-datasets/FABDEM');


// Reproject an image mosaic using a projection from one of the image tiles,
// rather than using the default projection returned by .mosaic().
var proj = elevation.first().select(0).projection();
var elevationReprojected = elevation.mosaic().setDefaultProjection(proj).unmask(0);
var slopeReprojected = ee.Terrain.slope(elevationReprojected);


var eleMedian = elevationReprojected.reduceNeighborhood({
    reducer: ee.Reducer.mean(),
    //kernel: ee.Kernel.gaussian(3000, 1000, "meters", true),
    kernel: ee.Kernel.circle(DEFAULT_ELE_REL_KERNEL, "meters", true),
    skipMasked: false
  });
  
var eleStd = elevationReprojected.reduceNeighborhood({
    reducer: ee.Reducer.stdDev(),
    //kernel: ee.Kernel.gaussian(3000, 1000, "meters", true),
    kernel: ee.Kernel.circle(DEFAULT_ELE_REL_KERNEL, "meters", true),
    skipMasked: false
  });
  
var eleRelative = eleMedian.subtract(elevationReprojected).divide(eleStd).resample('bilinear');


/*var eleRelativeMedian = eleRelative.reduceNeighborhood({
    reducer: ee.Reducer.mean(),
    //kernel: ee.Kernel.gaussian(3000, 1000, "meters", true),
    kernel: ee.Kernel.circle(10 * DEFAULT_ELE_REL_KERNEL, "meters", true),
    skipMasked: false
  });*/
  
var eleRelativeStd = eleRelative.reduceNeighborhood({
    reducer: ee.Reducer.stdDev(),
    //kernel: ee.Kernel.gaussian(3000, 1000, "meters", true),
    kernel: ee.Kernel.circle(3 * DEFAULT_ELE_REL_KERNEL, "meters", true),
    skipMasked: false
  });
  
var eleRelativeRelative = eleRelativeStd.resample('bilinear');


/*
var ele_kernel = ee.Kernel.circle({
  radius: 50, units: 'meters', magnitude: 1
});

var slope_kernel = ee.Kernel.circle({
  radius: 40, units: 'meters', magnitude: 1
});
var elevationSmoothened = elevationReprojected.convolve(ele_kernel);
var slopeSmoothened = slopeReprojected.convolve(slope_kernel);*/

var elevationSmoothened = elevationReprojected.resample('bicubic');
var slopeSmoothened = slopeReprojected.resample('bicubic');
var slope_inv = slopeSmoothened.multiply(-1).add(slope_vis.max);



var ele_rel_rel_layer = Map.addLayer(eleRelativeRelative, ele_rel_rel_vis, 'Detector de desmorrodouros - Dev.pad. da PTL-1km em 3km', false)

var elevation_layer = Map.addLayer(elevationSmoothened, elevation_vis, 'Elevação (FABDEM)', false);   
var slope_layer = Map.addLayer(slopeSmoothened, slope_vis, 'Declive (branco sobre escuro)', false);
var slope_inverted_layer = Map.addLayer(slope_inv, slope_vis, 'Declive (escuro sobre branco)', true);
var ele_rel_layer = Map.addLayer(eleRelative, ele_rel_vis, 'Posição Topográfica Local (PTL)', false)


var lbl_tileurl_license = ui.Label({value: "CC BY-NC-SA 4.0", style: {fontSize: '7px'}})
var lbl_tileurl = ui.Label({value: "", style: {fontSize: '7px', whiteSpace: 'normal', width:"90%"}})

function multiplySlope(ele_img, min, max, gamma){
  var blend = require('users/jja/public:blend.js');
  
  var slope_cor = slopeSmoothened.visualize({min: min, max: max, gamma: gamma});
  var slope_rgb = slope_cor.visualize({palette: ['#ffffff', '#000000'],
    forceRgbOutput:true
  });
  return blend.multiply(slope_rgb, ele_img);
}
var elevation_image = elevationSmoothened.visualize(elevation_vis);
var multiplied_slope = multiplySlope(elevation_image, slope_vis.min, slope_vis.max, slope_vis.gamma);

Map.addLayer(multiplied_slope, {}, 'Declive multiplicado por elevação (FABDEM)', true);

Map.add(fvLayer); // here because we want it to be the first

/* 
var slider_elevation_lower = ui.Slider({min: 0, 
                        max: 5000, 
                        value: 700, 
                        step: 25,
                        style: {width: '100%'}
});   


var slider_elevation_upper = ui.Slider({min: 0, 
                        max: 5000, 
                        value: 1200, 
                        step: 25,
                        style: {width: '100%'}
});  
*/

var slider_elevation_lower = ui.Textbox({value: 700, style: {width: '100%'}});
var slider_elevation_upper = ui.Textbox({value: 1200, style: {width: '100%'}});

var slider_slope = ui.Slider({min: 0, 
                        max: 100, 
                        value: 16, 
                        step: 2,
                        style: {width: '100%'}
});  

var slider_slope_gamma = ui.Slider({min: 0.0, 
                        max: 3.0, 
                        value: 1.0, 
                        step: 0.1,
                        style: {width: '100%'}
}); 

var slider_elevation_cycles = ui.Slider({min: 1.0, 
                        max: 5.0, 
                        value: DEFAULT_ELE_COLOR_CYCLES, 
                        step: 1.0,
                        style: {width: '100%'}
}); 

var slider_ele_rel_max = ui.Slider({min: 0.1, 
                        max: 3.0, 
                        value: DEFAULT_ELE_REL_MAX, 
                        step: 0.1,
                        style: {width: '100%'}
}); 

var slider_ele_rel_kernel = ui.Slider({min: 60, 
                        max: 1000, 
                        value: DEFAULT_ELE_REL_KERNEL, 
                        step: 30,
                        style: {width: '100%'}
}); 

                    
function updateLayer(value){
  var ele_rel_kernel = slider_ele_rel_kernel.getValue();
  var ele_rel_max = slider_ele_rel_max.getValue();
  
  var ele_lower = slider_elevation_lower.getValue();
  var ele_upper = slider_elevation_upper.getValue();
  var slope = slider_slope.getValue();
  var slope_gamma = slider_slope_gamma.getValue();
  var inverted_slope_gamma = slider_slope_gamma.getValue();
  
  ele_palette = elevation_palette(slider_elevation_cycles.getValue());

  var n_1 = N_FIXED_LAYERS + 1;
  Map.layers().get(n_1).setVisParams({"min": 0, "max": slope, "gamma": slope_gamma});
  Map.layers().get(n_1 - 1).setVisParams({"min": ele_lower, "max": ele_upper, "palette": ele_palette});
  
  var slope_viz = Map.layers().get(n_1).getVisParams(); 
  var ele_viz = Map.layers().get(n_1 - 1).getVisParams();
  ele_rel_vis['min'] = -ele_rel_max;
  ele_rel_vis['max'] = ele_rel_max;


  var slope_inverted = slopeSmoothened.multiply(-1).add(slope);
  var elevation_image = elevationSmoothened.visualize(ele_viz);
  multiplied_slope = multiplySlope(elevation_image, slope_viz.min, slope_viz.max, slope_viz.gamma);
  var n_2 = N_FIXED_LAYERS + N_MUTABLE_LAYERS - 1;
    
    
  var eleMedian = elevationReprojected.reduceNeighborhood({
      reducer: ee.Reducer.mean(),
      //kernel: ee.Kernel.gaussian(3000, 1000, "meters", true),
      kernel: ee.Kernel.circle(ele_rel_kernel, "meters", true),
      skipMasked: false
    });
    
  var eleStd = elevationReprojected.reduceNeighborhood({
      reducer: ee.Reducer.stdDev(),
      //kernel: ee.Kernel.gaussian(3000, 1000, "meters", true),
      kernel: ee.Kernel.circle(ele_rel_kernel, "meters", true),
      skipMasked: false
    });
    
  var eleRelative = eleMedian.subtract(elevationReprojected).divide(eleStd).resample('bicubic');  
  
  var l0 = Map.layers().get(n_2 + 1); // fvLayer
  var l1 = Map.layers().get(n_2); // fvLayer
  var l2 = Map.layers().get(n_2 - 1);
  var l3 = Map.layers().get(n_2 - 2);
  var v1 = l1.getShown();
  var v2 = l2.getShown();
  var v3 = l3.getShown();
  Map.remove(l0);
  Map.remove(l1);
  Map.remove(l2);
  Map.remove(l3);
  Map.addLayer(slope_inverted, slope_viz, 'Declive (escuro sobre branco)', v1);
  Map.addLayer(eleRelative, ele_rel_vis, 'Posição Topográfica Local (PTL)', v2)
  Map.addLayer(multiplied_slope, {}, 'Declive multiplicado por elevação (FABDEM)', v3);
  Map.add(fvLayer);
  Map.layers().get(n_2 - 2).setVisParams({"min": 0, "max": slope, "gamma": inverted_slope_gamma});
}        
                        
slider_elevation_lower.onChange(updateLayer);
slider_elevation_upper.onChange(updateLayer);
slider_slope.onSlide(updateLayer);
slider_slope_gamma.onSlide(updateLayer);
slider_elevation_cycles.onSlide(updateLayer);
slider_ele_rel_max.onSlide(updateLayer);
slider_ele_rel_kernel.onSlide(updateLayer);

function autoCompute(){
  
  var region_reducer = {
    reducer: ee.Reducer.percentile([2, 98]),
    geometry: ee.Geometry.Rectangle(Map.getBounds()),
    bestEffort: true
  };
  var slope_percs = slopeReprojected.reduceRegion(region_reducer)
  var ele_percs = elevationReprojected.reduceRegion(region_reducer)
  
  slider_elevation_upper
  var ele_lower = ele_percs.get('b1_p2').getInfo()
  var ele_upper =  ele_percs.get('b1_p98').getInfo()
  var slope_max = slope_percs.get('slope_p98').getInfo()
  slider_elevation_lower.setValue(ele_lower);
  slider_elevation_upper.setValue(ele_upper);
  slider_slope.setValue(slope_max);
  //Map.layers().get(1).setVisParams({"min": ele_lower, "max": ele_upper, "palette": palette})
}

function getTileMapURI(arg){
  var asset = null;
  if (arg == 1) {
    asset = multiplied_slope;
  }
  if (arg == 2)
  {
    asset = eleRelative.visualize(ele_rel_vis);
  } 
  var map_id = asset.getMapId();
  var tile_url = ee.data.getTileUrl(map_id);
  lbl_tileurl.setValue('Link XYZ:\n' + tile_url.split('undefined')[0] + "{z}/{x}/{y}");
}

function getTileMapURI1(){
  return getTileMapURI(1);
}
function getTileMapURI2(){
  return getTileMapURI(2);
}

var btn_compute = ui.Button({
  label: 'Estimar elevação mín/máx e declive máx',
  onClick: autoCompute
})

var btn_tilemap = ui.Button({
  label: 'Adquirir URI para telhas XYZ - elevação colorida',
  onClick: getTileMapURI1
})

var btn_tilemaprel = ui.Button({
  label: 'Adquirir URI para telhas XYZ - elevação relativa',
  onClick: getTileMapURI2
})



var panel = ui.Panel({style: {width: '20%'}})
     .add(ui.Label('Elevação mínima (m): '))
     .add(slider_elevation_lower)
     .add(ui.Label('Elevação máxima (m): '))
     .add(slider_elevation_upper)
     .add(ui.Label('Declive máximo (%): '))
     .add(slider_slope)
     .add(ui.Label('Correção gama no declive: '))
     .add(slider_slope_gamma)
     .add(ui.Label('Número de ciclos na paleta da elevação: '))
     .add(slider_elevation_cycles)
     .add(ui.Label('Max. desvio padroes para saturar PTL: '))
     .add(slider_ele_rel_max)
     .add(ui.Label('Tamanho do circulo para PTL: '))
     .add(slider_ele_rel_kernel)
     .add(btn_compute)
     .add(btn_tilemap)
     .add(btn_tilemaprel)
     .add(lbl_tileurl_license)
     .add(lbl_tileurl)
     

ui.root.add(panel);





     
